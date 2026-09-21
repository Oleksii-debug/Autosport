from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum, StrEnum
from pathlib import Path
from threading import Lock
from typing import Callable, Mapping, Protocol, Sequence

from .betfair_account_readonly import BetfairHttpTransport, BetfairReadOnlyError
from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.betfair_request_budget"
SCHEMA_VERSION = 1

LIST_MARKET_BOOK = "SportsAPING/v1.0/listMarketBook"
LIST_CURRENT_ORDERS = "SportsAPING/v1.0/listCurrentOrders"
LIST_CLEARED_ORDERS = "SportsAPING/v1.0/listClearedOrders"
LIST_MARKET_PROFIT_AND_LOSS = "SportsAPING/v1.0/listMarketProfitAndLoss"
LIST_MARKET_CATALOGUE = "SportsAPING/v1.0/listMarketCatalogue"
GET_ACCOUNT_FUNDS = "AccountAPING/v1.0/getAccountFunds"
GET_ACCOUNT_DETAILS = "AccountAPING/v1.0/getAccountDetails"

_READ_METHODS = frozenset(
    {
        LIST_MARKET_BOOK,
        LIST_CURRENT_ORDERS,
        LIST_CLEARED_ORDERS,
        LIST_MARKET_PROFIT_AND_LOSS,
        LIST_MARKET_CATALOGUE,
        GET_ACCOUNT_FUNDS,
        GET_ACCOUNT_DETAILS,
    }
)
_MAX_MARKET_DATA_POINTS = 200
_MAX_MARKET_BOOK_DISPATCHES_PER_MARKET_PER_SECOND = 5
_BACKPRESSURE_TOKENS = ("TOO_MANY_REQUESTS", "SERVICE_BUSY")
_HEX = frozenset("0123456789abcdef")


class BetfairRequestBudgetError(BetfairReadOnlyError):
    """A provider read was rejected locally before network dispatch."""


class BetfairRequestPriority(IntEnum):
    RECONCILIATION = 0
    SAFETY = 1
    EXECUTION_READ = 2
    MONITORING = 3
    BACKGROUND = 4


class BetfairRequestPool(StrEnum):
    SHARED_ORDER_READS = "SHARED_ORDER_READS"
    CLEARED_ORDERS = "CLEARED_ORDERS"
    MARKET_DATA = "MARKET_DATA"
    OTHER_READS = "OTHER_READS"


@dataclass(frozen=True, slots=True)
class BetfairRequestIntent:
    method: str
    priority: BetfairRequestPriority
    pool: BetfairRequestPool
    market_ids: tuple[str, ...] = ()
    request_weight_points: int | None = None

    def __post_init__(self) -> None:
        _text(self.method, "method")
        if self.method not in _READ_METHODS:
            raise BetfairRequestBudgetError("request budget accepts Betfair read methods only")
        if not isinstance(self.priority, BetfairRequestPriority):
            raise TypeError("priority must be BetfairRequestPriority")
        if not isinstance(self.pool, BetfairRequestPool):
            raise TypeError("pool must be BetfairRequestPool")
        _text_tuple(self.market_ids, "market_ids")
        if self.method == LIST_MARKET_BOOK:
            if not self.market_ids:
                raise BetfairRequestBudgetError("listMarketBook requires at least one market id")
            if self.request_weight_points is None:
                raise BetfairRequestBudgetError(
                    "listMarketBook requires explicit total request-weight evidence"
                )
            if (
                type(self.request_weight_points) is not int
                or not 1 <= self.request_weight_points <= _MAX_MARKET_DATA_POINTS
            ):
                raise BetfairRequestBudgetError(
                    "listMarketBook request weight must be an integer in 1..200"
                )
        elif self.request_weight_points is not None:
            raise BetfairRequestBudgetError(
                "request_weight_points is only valid for listMarketBook"
            )


class MarketDataWeightResolver(Protocol):
    def __call__(self, params: Mapping[str, object]) -> int: ...


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairRequestBudgetError("budget state is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise BetfairRequestBudgetError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise BetfairRequestBudgetError(f"{name} must be valid UTF-8") from exc
    return value


def _text_tuple(value: object, name: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise BetfairRequestBudgetError(f"{name} must be a tuple")
    result = tuple(_text(item, name) for item in value)
    if len(result) != len(set(result)):
        raise BetfairRequestBudgetError(f"{name} contains duplicate values")
    return result


def _instant(value: object, name: str) -> datetime:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairRequestBudgetError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairRequestBudgetError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BetfairRequestBudgetError("clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BetfairRequestBudgetError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise BetfairRequestBudgetError(f"non-finite JSON number is not allowed: {value}")


def _parse_json_bytes(payload: bytes, *, context: str) -> object:
    if not isinstance(payload, bytes):
        raise BetfairRequestBudgetError(f"{context} must be bytes")
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except UnicodeDecodeError as exc:
        raise BetfairRequestBudgetError(f"{context} must be UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise BetfairRequestBudgetError(f"{context} must be valid JSON") from exc


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BetfairRequestBudgetError(f"{name} must be a JSON object")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BetfairRequestBudgetError(f"{name} must be a JSON array")
    return value


def _pool_for(method: str, params: Mapping[str, object]) -> BetfairRequestPool:
    if method == LIST_CLEARED_ORDERS:
        return BetfairRequestPool.CLEARED_ORDERS
    if method in {LIST_CURRENT_ORDERS, LIST_MARKET_PROFIT_AND_LOSS}:
        return BetfairRequestPool.SHARED_ORDER_READS
    if method == LIST_MARKET_BOOK:
        if params.get("orderProjection") is not None or params.get("matchProjection") is not None:
            return BetfairRequestPool.SHARED_ORDER_READS
        return BetfairRequestPool.MARKET_DATA
    return BetfairRequestPool.OTHER_READS


def _priority_for(method: str) -> BetfairRequestPriority:
    if method in {LIST_CURRENT_ORDERS, LIST_CLEARED_ORDERS}:
        return BetfairRequestPriority.RECONCILIATION
    if method in {GET_ACCOUNT_FUNDS, LIST_MARKET_PROFIT_AND_LOSS}:
        return BetfairRequestPriority.EXECUTION_READ
    if method == LIST_MARKET_BOOK:
        return BetfairRequestPriority.MONITORING
    return BetfairRequestPriority.BACKGROUND


def _market_ids(params: Mapping[str, object]) -> tuple[str, ...]:
    raw = params.get("marketIds")
    if raw is None:
        return ()
    values = tuple(_text(item, "market_id") for item in _sequence(raw, "marketIds"))
    if len(values) != len(set(values)):
        raise BetfairRequestBudgetError("marketIds contains duplicate values")
    return values


def intent_from_rpc_body(
    body: bytes,
    *,
    market_data_weight_resolver: MarketDataWeightResolver | None = None,
) -> BetfairRequestIntent:
    envelope = _mapping(_parse_json_bytes(body, context="Betfair RPC request"), "RPC request")
    if envelope.get("jsonrpc") != "2.0":
        raise BetfairRequestBudgetError("Betfair RPC request has invalid jsonrpc version")
    method = _text(envelope.get("method"), "method")
    if method not in _READ_METHODS:
        raise BetfairRequestBudgetError(
            "budgeted transport refuses provider mutation or unknown RPC method"
        )
    params = _mapping(envelope.get("params"), "params")
    markets = _market_ids(params)
    weight: int | None = None
    if method == LIST_MARKET_BOOK:
        if market_data_weight_resolver is None:
            raise BetfairRequestBudgetError(
                "listMarketBook requires a total-weight resolver before dispatch"
            )
        weight = market_data_weight_resolver(params)
        if type(weight) is not int:
            raise BetfairRequestBudgetError(
                "market-data weight resolver must return integer total request points"
            )
    return BetfairRequestIntent(
        method=method,
        priority=_priority_for(method),
        pool=_pool_for(method, params),
        market_ids=markets,
        request_weight_points=weight,
    )


_PROCESS_LOCK = Lock()
_ACTIVE_BY_PATH: dict[str, dict[BetfairRequestPool, int]] = {}


class _BudgetLease:
    def __init__(self, budget: "BetfairRequestBudget", pool: BetfairRequestPool) -> None:
        self._budget = budget
        self._pool = pool
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._budget._release(self._pool)

    def __enter__(self) -> "_BudgetLease":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class BetfairRequestBudget:
    """Durable, fail-closed local admission budget for Betfair read requests.

    Provider writes are deliberately outside this authority. The durable state
    contains only scheduling metadata (timestamps/backoff), never credentials,
    request bodies or provider payloads.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        shared_capacity: int = 3,
        reserved_reconciliation_slots: int = 1,
        cleared_capacity: int = 2,
        market_data_capacity: int = 4,
        other_read_capacity: int = 2,
        backoff_base_seconds: float = 0.25,
        backoff_max_seconds: float = 8.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(state_path)
        if not self.path.name:
            raise ValueError("state_path must name a file")
        for value, name in (
            (shared_capacity, "shared_capacity"),
            (cleared_capacity, "cleared_capacity"),
            (market_data_capacity, "market_data_capacity"),
            (other_read_capacity, "other_read_capacity"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            type(reserved_reconciliation_slots) is not int
            or reserved_reconciliation_slots < 1
            or reserved_reconciliation_slots >= shared_capacity
        ):
            raise ValueError(
                "reserved_reconciliation_slots must be in 1..shared_capacity-1"
            )
        for value, name in (
            (backoff_base_seconds, "backoff_base_seconds"),
            (backoff_max_seconds, "backoff_max_seconds"),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive")
        if backoff_max_seconds < backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_base_seconds")
        self.shared_capacity = shared_capacity
        self.reserved_reconciliation_slots = reserved_reconciliation_slots
        self._capacities = {
            BetfairRequestPool.SHARED_ORDER_READS: shared_capacity,
            BetfairRequestPool.CLEARED_ORDERS: cleared_capacity,
            BetfairRequestPool.MARKET_DATA: market_data_capacity,
            BetfairRequestPool.OTHER_READS: other_read_capacity,
        }
        self.backoff_base_seconds = float(backoff_base_seconds)
        self.backoff_max_seconds = float(backoff_max_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._path_key = str(self.path.resolve())
        with _PROCESS_LOCK:
            _ACTIVE_BY_PATH.setdefault(
                self._path_key,
                {pool: 0 for pool in BetfairRequestPool},
            )
        with WorkspaceEconomicLock(self.path.parent):
            if self.path.exists():
                self._read_locked()
            else:
                self._write_locked(self._pristine_state())

    @staticmethod
    def _pristine_state() -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "last_seen_at": None,
            "market_book_dispatches": {},
            "backpressure": {
                pool.value: {"level": 0, "until": None}
                for pool in BetfairRequestPool
            },
        }

    def _write_locked(self, state: Mapping[str, object]) -> None:
        bare = dict(state)
        bare.pop("state_sha256", None)
        atomic_write_json(self.path, {**bare, "state_sha256": _digest(bare)})

    def _read_locked(self) -> dict[str, object]:
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise BetfairRequestBudgetError("cannot read request-budget state") from exc
        decoded = _mapping(_parse_json_bytes(raw, context="request-budget state"), "state")
        expected_keys = {
            "schema",
            "schema_version",
            "last_seen_at",
            "market_book_dispatches",
            "backpressure",
            "state_sha256",
        }
        if set(decoded) != expected_keys:
            raise BetfairRequestBudgetError("request-budget state has unexpected fields")
        if decoded["schema"] != SCHEMA or decoded["schema_version"] != SCHEMA_VERSION:
            raise BetfairRequestBudgetError("unsupported request-budget state schema")
        digest = decoded["state_sha256"]
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(char not in _HEX for char in digest)
        ):
            raise BetfairRequestBudgetError("request-budget state digest is malformed")
        bare = {key: value for key, value in decoded.items() if key != "state_sha256"}
        if digest != _digest(bare):
            raise BetfairRequestBudgetError("request-budget state digest mismatch")
        last_seen = decoded["last_seen_at"]
        if last_seen is not None:
            _instant(last_seen, "last_seen_at")
        dispatches = _mapping(decoded["market_book_dispatches"], "market_book_dispatches")
        for market_id, values in dispatches.items():
            _text(market_id, "market_id")
            for item in _sequence(values, "market dispatch timestamps"):
                timestamp = _text(item, "market dispatch timestamp")
                _instant(timestamp, "market dispatch timestamp")
        backpressure = _mapping(decoded["backpressure"], "backpressure")
        if set(backpressure) != {pool.value for pool in BetfairRequestPool}:
            raise BetfairRequestBudgetError("backpressure state has invalid pool set")
        for pool in BetfairRequestPool:
            entry = _mapping(backpressure[pool.value], "backpressure entry")
            if set(entry) != {"level", "until"}:
                raise BetfairRequestBudgetError(
                    "backpressure entry has unexpected fields"
                )
            level = entry["level"]
            if type(level) is not int or level < 0:
                raise BetfairRequestBudgetError("backpressure level is invalid")
            until = entry["until"]
            if until is not None:
                _instant(until, "backpressure until")
        return dict(decoded)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BetfairRequestBudgetError("clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _ensure_monotonic(state: Mapping[str, object], now: datetime) -> None:
        last_seen = state["last_seen_at"]
        if last_seen is not None and now < _instant(last_seen, "last_seen_at"):
            raise BetfairRequestBudgetError(
                "request-budget clock moved backwards across durable state"
            )

    def _check_backpressure(
        self,
        state: Mapping[str, object],
        pool: BetfairRequestPool,
        now: datetime,
    ) -> None:
        backpressure = _mapping(state["backpressure"], "backpressure")
        entry = _mapping(backpressure[pool.value], "backpressure entry")
        until = entry["until"]
        if until is not None and now < _instant(until, "backpressure until"):
            raise BetfairRequestBudgetError(
                f"Betfair {pool.value} pool is in durable provider backpressure cooldown"
            )

    def _check_and_record_market_rate(
        self,
        state: dict[str, object],
        intent: BetfairRequestIntent,
        now: datetime,
    ) -> None:
        if intent.method != LIST_MARKET_BOOK:
            return
        dispatches = dict(
            _mapping(state["market_book_dispatches"], "market_book_dispatches")
        )
        cutoff = now - timedelta(seconds=1)
        for market_id in intent.market_ids:
            raw_history = _sequence(
                dispatches.get(market_id, []),
                "market dispatch timestamps",
            )
            recent = [
                _timestamp(ts)
                for item in raw_history
                for ts in [_instant(item, "market dispatch timestamp")]
                if ts > cutoff
            ]
            if len(recent) >= _MAX_MARKET_BOOK_DISPATCHES_PER_MARKET_PER_SECOND:
                raise BetfairRequestBudgetError(
                    f"listMarketBook local rate limit reached for market {market_id}"
                )
            recent.append(_timestamp(now))
            dispatches[market_id] = recent
        state["market_book_dispatches"] = dispatches

    def _reserve_process_slot(self, intent: BetfairRequestIntent) -> None:
        with _PROCESS_LOCK:
            active = _ACTIVE_BY_PATH[self._path_key]
            current = active[intent.pool]
            limit = self._capacities[intent.pool]
            if (
                intent.pool is BetfairRequestPool.SHARED_ORDER_READS
                and intent.priority > BetfairRequestPriority.SAFETY
            ):
                limit -= self.reserved_reconciliation_slots
            if current >= limit:
                raise BetfairRequestBudgetError(
                    f"Betfair {intent.pool.value} local concurrency budget is exhausted"
                )
            active[intent.pool] = current + 1

    def _release(self, pool: BetfairRequestPool) -> None:
        with _PROCESS_LOCK:
            active = _ACTIVE_BY_PATH.get(self._path_key)
            if active is None or active[pool] <= 0:
                raise RuntimeError("request-budget lease release underflow")
            active[pool] -= 1

    def _validate_intent_classification(self, intent: BetfairRequestIntent) -> None:
        expected_priority = _priority_for(intent.method)
        if intent.priority is not expected_priority:
            raise BetfairRequestBudgetError(
                "request priority does not match canonical Betfair method classification"
            )
        if intent.method == LIST_MARKET_BOOK:
            if intent.pool not in {
                BetfairRequestPool.MARKET_DATA,
                BetfairRequestPool.SHARED_ORDER_READS,
            }:
                raise BetfairRequestBudgetError(
                    "listMarketBook request pool is not canonical"
                )
            return
        expected_pool = _pool_for(intent.method, {})
        if intent.pool is not expected_pool:
            raise BetfairRequestBudgetError(
                "request pool does not match canonical Betfair method classification"
            )

    def acquire(self, intent: BetfairRequestIntent) -> _BudgetLease:
        if not isinstance(intent, BetfairRequestIntent):
            raise TypeError("intent must be BetfairRequestIntent")
        self._validate_intent_classification(intent)
        now = self._now()
        reserved = False
        try:
            self._reserve_process_slot(intent)
            reserved = True
            with WorkspaceEconomicLock(self.path.parent):
                state = self._read_locked()
                self._ensure_monotonic(state, now)
                self._check_backpressure(state, intent.pool, now)
                self._check_and_record_market_rate(state, intent, now)
                state["last_seen_at"] = _timestamp(now)
                self._write_locked(state)
            return _BudgetLease(self, intent.pool)
        except BaseException:
            if reserved:
                self._release(intent.pool)
            raise

    def record_backpressure(self, pool: BetfairRequestPool) -> None:
        if not isinstance(pool, BetfairRequestPool):
            raise TypeError("pool must be BetfairRequestPool")
        now = self._now()
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read_locked()
            self._ensure_monotonic(state, now)
            backpressure = dict(_mapping(state["backpressure"], "backpressure"))
            current = _mapping(backpressure[pool.value], "backpressure entry")
            level = int(current["level"]) + 1
            exponent = min(level - 1, 20)
            delay = min(
                self.backoff_max_seconds,
                self.backoff_base_seconds * (2**exponent),
            )
            backpressure[pool.value] = {
                "level": level,
                "until": _timestamp(now + timedelta(seconds=delay)),
            }
            state["backpressure"] = backpressure
            state["last_seen_at"] = _timestamp(now)
            self._write_locked(state)

    def record_success(self, pool: BetfairRequestPool) -> None:
        if not isinstance(pool, BetfairRequestPool):
            raise TypeError("pool must be BetfairRequestPool")
        now = self._now()
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read_locked()
            self._ensure_monotonic(state, now)
            backpressure = dict(_mapping(state["backpressure"], "backpressure"))
            entry = _mapping(backpressure[pool.value], "backpressure entry")
            until = entry["until"]
            cooldown_expired = (
                until is None or now >= _instant(until, "backpressure until")
            )
            if entry["level"] != 0 and cooldown_expired:
                backpressure[pool.value] = {"level": 0, "until": None}
                state["backpressure"] = backpressure
            state["last_seen_at"] = _timestamp(now)
            self._write_locked(state)


def _provider_backpressure(payload: bytes) -> bool | None:
    try:
        decoded = _parse_json_bytes(payload, context="Betfair RPC response")
    except BetfairRequestBudgetError:
        return None
    if not isinstance(decoded, Mapping):
        return None
    error = decoded.get("error")
    if error is None:
        return False
    if not isinstance(error, Mapping):
        return None
    parts: list[str] = []
    for key in ("message", "data"):
        value = error.get(key)
        if isinstance(value, str):
            parts.append(value.upper())
        elif isinstance(value, Mapping):
            parts.extend(
                str(item).upper()
                for item in value.values()
                if isinstance(item, (str, int))
            )
    joined = " ".join(parts)
    return any(token in joined for token in _BACKPRESSURE_TOKENS)


class BudgetedBetfairReadTransport:
    """BetfairHttpTransport decorator that performs one admitted read, never retries."""

    def __init__(
        self,
        wrapped: BetfairHttpTransport,
        budget: BetfairRequestBudget,
        *,
        market_data_weight_resolver: MarketDataWeightResolver | None = None,
    ) -> None:
        if not hasattr(wrapped, "post") or not callable(getattr(wrapped, "post")):
            raise TypeError("wrapped must implement BetfairHttpTransport.post")
        if not isinstance(budget, BetfairRequestBudget):
            raise TypeError("budget must be BetfairRequestBudget")
        self._wrapped = wrapped
        self._budget = budget
        self._market_data_weight_resolver = market_data_weight_resolver

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        intent = intent_from_rpc_body(
            body,
            market_data_weight_resolver=self._market_data_weight_resolver,
        )
        with self._budget.acquire(intent):
            try:
                payload = self._wrapped.post(
                    url,
                    headers=headers,
                    body=body,
                    timeout_seconds=timeout_seconds,
                )
            except BetfairReadOnlyError as exc:
                if "status 429" in str(exc).lower():
                    self._budget.record_backpressure(intent.pool)
                raise
            if not isinstance(payload, bytes):
                return payload
            pressure = _provider_backpressure(payload)
            if pressure is True:
                self._budget.record_backpressure(intent.pool)
            elif pressure is False:
                self._budget.record_success(intent.pool)
            return payload


__all__ = [
    "BetfairRequestBudget",
    "BetfairRequestBudgetError",
    "BetfairRequestIntent",
    "BetfairRequestPool",
    "BetfairRequestPriority",
    "BudgetedBetfairReadTransport",
    "GET_ACCOUNT_DETAILS",
    "GET_ACCOUNT_FUNDS",
    "LIST_CLEARED_ORDERS",
    "LIST_CURRENT_ORDERS",
    "LIST_MARKET_BOOK",
    "LIST_MARKET_CATALOGUE",
    "LIST_MARKET_PROFIT_AND_LOSS",
    "intent_from_rpc_body",
]
