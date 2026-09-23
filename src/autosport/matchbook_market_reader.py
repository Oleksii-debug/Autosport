"""Fail-closed Matchbook live price read evidence.

This module is deliberately read-only and transport-neutral.  It binds the exact
Matchbook GET-prices request semantics and raw response bytes into deterministic
market-intelligence evidence without granting provider-write, execution,
settlement, FX, completeness, or real-money authority.

Official reference checked 2026-09-23:
https://developers.matchbook.com/reference/get-prices
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode


_I64_MAX = (1 << 63) - 1
_MAX_RAW_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_DEPTH = 100
_MAX_POLL_SCOPES = 100_000
_ALLOWED_CURRENCIES = frozenset({"USD", "EUR", "GBP", "AUD", "CAD", "HKD"})


class MatchbookMarketReadError(ValueError):
    """Matchbook read evidence is malformed, ambiguous, or unsafe to consume."""


class MatchbookEnvironment(StrEnum):
    PRODUCTION = "production"


class MatchbookExchangeType(StrEnum):
    BACK_LAY = "back-lay"
    BINARY = "binary"


class MatchbookOddsType(StrEnum):
    DECIMAL = "DECIMAL"
    US = "US"
    HK = "HK"
    MALAY = "MALAY"
    INDO = "INDO"
    PERCENT = "%"


class MatchbookPriceMode(StrEnum):
    EXPANDED = "expanded"
    AGGREGATED = "aggregated"


class MatchbookSide(StrEnum):
    BACK = "back"
    LAY = "lay"
    WIN = "win"
    LOSE = "lose"


class MatchbookMarketState(StrEnum):
    OPEN = "open"
    SUSPENDED = "suspended"


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise MatchbookMarketReadError("value is outside canonical JSON domain") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _text(value: object, field: str, *, max_bytes: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise MatchbookMarketReadError(f"{field} must be non-empty trimmed text")
    if any(ord(char) < 32 for char in value):
        raise MatchbookMarketReadError(f"{field} contains control characters")
    if len(value.encode("utf-8")) > max_bytes:
        raise MatchbookMarketReadError(f"{field} is too large")
    return value


def _positive_i64(value: object, field: str) -> str:
    if type(value) is int:
        parsed = value
    elif (
        type(value) is str
        and value
        and value.isascii()
        and value.isdigit()
        and (len(value) == 1 or value[0] != "0")
    ):
        parsed = int(value)
    else:
        raise MatchbookMarketReadError(
            f"{field} must be a canonical positive signed-64 integer"
        )
    if not 0 < parsed <= _I64_MAX:
        raise MatchbookMarketReadError(
            f"{field} must be a canonical positive signed-64 integer"
        )
    return str(parsed)


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise MatchbookMarketReadError(f"{field} must use exact decimal ingress")
    if isinstance(value, Decimal):
        parsed = value
    elif type(value) is int:
        parsed = Decimal(value)
    elif type(value) is str:
        if not value or value != value.strip():
            raise MatchbookMarketReadError(f"{field} must be canonical decimal text")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise MatchbookMarketReadError(f"{field} is not a decimal") from exc
    else:
        raise MatchbookMarketReadError(f"{field} must use exact decimal ingress")
    if not parsed.is_finite():
        raise MatchbookMarketReadError(f"{field} must be finite")
    if parsed < 0 or (positive and parsed <= 0):
        relation = "positive" if positive else "non-negative"
        raise MatchbookMarketReadError(f"{field} must be {relation}")
    return parsed


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field, max_bytes=128)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatchbookMarketReadError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MatchbookMarketReadError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _strict_json(raw_response: bytes) -> object:
    if type(raw_response) is not bytes or not raw_response:
        raise MatchbookMarketReadError("raw_response must be non-empty bytes")
    if len(raw_response) > _MAX_RAW_RESPONSE_BYTES:
        raise MatchbookMarketReadError("raw_response exceeds bounded size")
    try:
        text = raw_response.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MatchbookMarketReadError("raw_response must be UTF-8") from exc

    def object_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise MatchbookMarketReadError(f"duplicate JSON key {key!r}")
            out[key] = value
        return out

    def reject_constant(value: str) -> None:
        raise MatchbookMarketReadError(f"non-standard JSON constant {value!r}")

    try:
        return json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise MatchbookMarketReadError("raw_response is invalid JSON") from exc


@dataclass(frozen=True, slots=True)
class MatchbookMarketReadScope:
    """Exact identity of one Matchbook GET-prices observation."""

    account_ref: str
    environment: MatchbookEnvironment
    sport_id: str | int
    event_id: str | int
    market_id: str | int
    runner_id: str | int
    side: MatchbookSide
    exchange_type: MatchbookExchangeType
    odds_type: MatchbookOddsType
    currency: str
    price_mode: MatchbookPriceMode
    depth: int
    minimum_liquidity: Decimal | str | int
    exclude_mirrored_prices: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_ref", _text(self.account_ref, "account_ref"))
        if type(self.environment) is not MatchbookEnvironment:
            raise MatchbookMarketReadError("environment must be explicit MatchbookEnvironment")
        for field in ("sport_id", "event_id", "market_id", "runner_id"):
            object.__setattr__(self, field, _positive_i64(getattr(self, field), field))
        if type(self.side) is not MatchbookSide:
            raise MatchbookMarketReadError("side must be explicit MatchbookSide")
        if type(self.exchange_type) is not MatchbookExchangeType:
            raise MatchbookMarketReadError("exchange_type must be explicit")
        if type(self.odds_type) is not MatchbookOddsType:
            raise MatchbookMarketReadError("odds_type must be explicit")
        if self.odds_type is not MatchbookOddsType.DECIMAL:
            raise MatchbookMarketReadError(
                "live price reader currently supports exact DECIMAL odds only"
            )
        currency = _text(self.currency, "currency", max_bytes=8).upper()
        if currency not in _ALLOWED_CURRENCIES:
            raise MatchbookMarketReadError("currency is not supported by Matchbook read contract")
        object.__setattr__(self, "currency", currency)
        if type(self.price_mode) is not MatchbookPriceMode:
            raise MatchbookMarketReadError("price_mode must be explicit")
        if type(self.depth) is not int or not 1 <= self.depth <= _MAX_DEPTH:
            raise MatchbookMarketReadError(f"depth must be in 1..{_MAX_DEPTH}")
        minimum = _decimal(self.minimum_liquidity, "minimum_liquidity", positive=True)
        object.__setattr__(self, "minimum_liquidity", minimum)
        if type(self.exclude_mirrored_prices) is not bool:
            raise MatchbookMarketReadError("exclude_mirrored_prices must be explicit bool")
        if self.exchange_type is MatchbookExchangeType.BACK_LAY and self.side not in {
            MatchbookSide.BACK,
            MatchbookSide.LAY,
        }:
            raise MatchbookMarketReadError("back-lay exchange_type requires back or lay side")
        if self.exchange_type is MatchbookExchangeType.BINARY and self.side not in {
            MatchbookSide.WIN,
            MatchbookSide.LOSE,
        }:
            raise MatchbookMarketReadError("binary exchange_type requires win or lose side")

    @property
    def path(self) -> str:
        return (
            f"/edge/rest/events/{self.event_id}/markets/{self.market_id}"
            f"/runners/{self.runner_id}/prices"
        )

    @property
    def query_items(self) -> tuple[tuple[str, str], ...]:
        return (
            ("exchange-type", self.exchange_type.value),
            ("odds-type", self.odds_type.value),
            ("depth", str(self.depth)),
            ("side", self.side.value),
            ("currency", self.currency),
            ("minimum-liquidity", _decimal_text(self.minimum_liquidity)),
            ("price-mode", self.price_mode.value),
            (
                "exclude-mirrored-prices",
                "true" if self.exclude_mirrored_prices else "false",
            ),
        )

    @property
    def request_target(self) -> str:
        return f"{self.path}?{urlencode(self.query_items)}"

    @property
    def scope_sha256(self) -> str:
        return _digest(
            {
                "v": 1,
                "provider": "matchbook",
                "account_ref": self.account_ref,
                "environment": self.environment.value,
                "sport_id": self.sport_id,
                "event_id": self.event_id,
                "market_id": self.market_id,
                "runner_id": self.runner_id,
                "side": self.side.value,
                "exchange_type": self.exchange_type.value,
                "odds_type": self.odds_type.value,
                "currency": self.currency,
                "price_mode": self.price_mode.value,
                "depth": self.depth,
                "minimum_liquidity": _decimal_text(self.minimum_liquidity),
                "exclude_mirrored_prices": self.exclude_mirrored_prices,
                "method": "GET",
                "path": self.path,
            }
        )


@dataclass(frozen=True, slots=True)
class MatchbookPriceLevel:
    side: MatchbookSide
    odds: Decimal
    available_amount: Decimal
    level_index: int
    aggregated_tail: bool

    def __post_init__(self) -> None:
        if type(self.side) is not MatchbookSide:
            raise MatchbookMarketReadError("price side is invalid")
        object.__setattr__(self, "odds", _decimal(self.odds, "odds", positive=True))
        object.__setattr__(
            self,
            "available_amount",
            _decimal(self.available_amount, "available_amount"),
        )
        if type(self.level_index) is not int or self.level_index < 0:
            raise MatchbookMarketReadError("level_index must be a non-negative integer")
        if type(self.aggregated_tail) is not bool:
            raise MatchbookMarketReadError("aggregated_tail must be bool")

    def payload(self) -> dict[str, object]:
        return {
            "side": self.side.value,
            "odds": _decimal_text(self.odds),
            "available_amount": _decimal_text(self.available_amount),
            "level_index": self.level_index,
            "aggregated_tail": self.aggregated_tail,
        }


@dataclass(frozen=True, slots=True)
class MatchbookMarketSnapshot:
    scope: MatchbookMarketReadScope
    market_state: MatchbookMarketState
    observed_at: str
    raw_response_sha256: str
    raw_response_size_bytes: int
    prices: tuple[MatchbookPriceLevel, ...]
    snapshot_sha256: str

    provider_origin = "matchbook"
    provider_origin_verified = False
    provider_event_time = None
    provider_write_authority = False
    execution_authority = False
    settlement_authority = False
    fx_authority = False
    authoritative_absence = False
    atomic_market_snapshot = False

    @property
    def actionable_market_state(self) -> bool:
        return self.market_state is MatchbookMarketState.OPEN

    @property
    def raw_depth_qualified(self) -> bool:
        return self.scope.price_mode is MatchbookPriceMode.EXPANDED

    def require_raw_depth(self) -> tuple[MatchbookPriceLevel, ...]:
        if not self.raw_depth_qualified:
            raise MatchbookMarketReadError(
                "aggregated Matchbook prices are not raw market-depth evidence"
            )
        return self.prices


def _price_rows(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        rows = payload.get("prices")
        if isinstance(rows, list):
            return rows
    raise MatchbookMarketReadError("price response must be prices[] or a JSON list")


def parse_matchbook_price_snapshot(
    scope: MatchbookMarketReadScope,
    raw_response: bytes,
    *,
    observed_at: str,
    market_state: MatchbookMarketState,
) -> MatchbookMarketSnapshot:
    """Parse one synthetic/live raw Matchbook response into immutable read evidence."""

    if type(scope) is not MatchbookMarketReadScope:
        raise MatchbookMarketReadError("scope must be MatchbookMarketReadScope")
    if type(market_state) is not MatchbookMarketState:
        raise MatchbookMarketReadError("market_state must be explicit known state")
    observed = _timestamp(observed_at, "observed_at")
    payload = _strict_json(raw_response)
    rows = _price_rows(payload)
    max_rows = min(scope.depth, 3) if scope.price_mode is MatchbookPriceMode.AGGREGATED else scope.depth
    if len(rows) > max_rows:
        raise MatchbookMarketReadError("provider returned more price rows than requested semantics allow")

    prices: list[MatchbookPriceLevel] = []
    seen: set[tuple[str, str, str]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise MatchbookMarketReadError("price row must be an object")
        try:
            side = MatchbookSide(_text(row.get("side"), "price.side", max_bytes=16))
        except ValueError as exc:
            raise MatchbookMarketReadError("price.side is unknown") from exc
        if side is not scope.side:
            raise MatchbookMarketReadError("response price side is outside explicit request scope")
        odds = _decimal(row.get("odds"), "price.odds", positive=True)
        available = _decimal(row.get("available-amount"), "price.available-amount")
        if available < scope.minimum_liquidity:
            raise MatchbookMarketReadError(
                "response contains liquidity below explicit minimum-liquidity scope"
            )
        identity = (side.value, _decimal_text(odds), _decimal_text(available))
        if identity in seen:
            raise MatchbookMarketReadError("duplicate exact price row")
        seen.add(identity)
        prices.append(
            MatchbookPriceLevel(
                side=side,
                odds=odds,
                available_amount=available,
                level_index=index,
                aggregated_tail=(
                    scope.price_mode is MatchbookPriceMode.AGGREGATED and index >= 2
                ),
            )
        )

    raw_sha = hashlib.sha256(raw_response).hexdigest()
    truth = {
        "provider_write_authority": False,
        "execution_authority": False,
        "settlement_authority": False,
        "fx_authority": False,
        "authoritative_absence": False,
        "atomic_market_snapshot": False,
        "raw_depth_qualified": scope.price_mode is MatchbookPriceMode.EXPANDED,
    }
    snapshot_sha = _digest(
        {
            "v": 1,
            "provider": "matchbook",
            "scope": scope.scope_sha256,
            "market_state": market_state.value,
            "observed_at": observed,
            "raw_response_sha256": raw_sha,
            "raw_response_size_bytes": len(raw_response),
            "prices": [price.payload() for price in prices],
            "truth": truth,
        }
    )
    return MatchbookMarketSnapshot(
        scope=scope,
        market_state=market_state,
        observed_at=observed,
        raw_response_sha256=raw_sha,
        raw_response_size_bytes=len(raw_response),
        prices=tuple(prices),
        snapshot_sha256=snapshot_sha,
    )


def require_current_open_snapshot(
    snapshot: MatchbookMarketSnapshot,
    *,
    as_of: str,
    max_age_seconds: int,
) -> MatchbookMarketSnapshot:
    """Fail closed before an observation can be consumed as current opportunity data."""

    if type(snapshot) is not MatchbookMarketSnapshot:
        raise MatchbookMarketReadError("snapshot is invalid")
    if type(max_age_seconds) is not int or isinstance(max_age_seconds, bool) or max_age_seconds < 0:
        raise MatchbookMarketReadError("max_age_seconds must be a non-negative integer")
    if snapshot.market_state is not MatchbookMarketState.OPEN:
        raise MatchbookMarketReadError("suspended market evidence is not actionable")
    current = _timestamp(as_of, "as_of")
    observed_dt = _timestamp_dt(snapshot.observed_at)
    current_dt = _timestamp_dt(current)
    if observed_dt > current_dt:
        raise MatchbookMarketReadError("future observation cannot be current evidence")
    if (current_dt - observed_dt).total_seconds() > max_age_seconds:
        raise MatchbookMarketReadError("Matchbook observation is stale")
    return snapshot


def compare_displayed_liquidity(
    left: MatchbookMarketSnapshot,
    right: MatchbookMarketSnapshot,
) -> Decimal:
    """Compare displayed liquidity only when denomination is exactly the same."""

    if type(left) is not MatchbookMarketSnapshot or type(right) is not MatchbookMarketSnapshot:
        raise MatchbookMarketReadError("liquidity comparison requires Matchbook snapshots")
    if left.scope.currency != right.scope.currency:
        raise MatchbookMarketReadError(
            "cross-currency liquidity comparison requires a separate exact FX authority"
        )
    if left.scope.side is not right.scope.side:
        raise MatchbookMarketReadError("liquidity comparison requires one side")
    left_total = sum((price.available_amount for price in left.prices), Decimal(0))
    right_total = sum((price.available_amount for price in right.prices), Decimal(0))
    return right_total - left_total


@dataclass(frozen=True, slots=True)
class MatchbookPollCheckpoint:
    universe_sha256: str
    next_index: int
    cycle: int

    def __post_init__(self) -> None:
        if type(self.universe_sha256) is not str or len(self.universe_sha256) != 64:
            raise MatchbookMarketReadError("universe_sha256 is invalid")
        if any(char not in "0123456789abcdef" for char in self.universe_sha256):
            raise MatchbookMarketReadError("universe_sha256 is invalid")
        if type(self.next_index) is not int or self.next_index < 0:
            raise MatchbookMarketReadError("next_index must be non-negative")
        if type(self.cycle) is not int or self.cycle < 0:
            raise MatchbookMarketReadError("cycle must be non-negative")


@dataclass(frozen=True, slots=True)
class MatchbookPollPlan:
    scope_sha256s: tuple[str, ...]
    checkpoint: MatchbookPollCheckpoint
    request_budget: int
    universe_size: int


def plan_matchbook_poll_batch(
    scope_sha256s: Sequence[str],
    *,
    request_budget: int,
    checkpoint: MatchbookPollCheckpoint | None = None,
) -> MatchbookPollPlan:
    """Return a deterministic bounded slice; a restart cannot expand one call past budget."""

    if isinstance(scope_sha256s, (str, bytes)) or not isinstance(scope_sha256s, Sequence):
        raise MatchbookMarketReadError("scope_sha256s must be a sequence")
    if not scope_sha256s or len(scope_sha256s) > _MAX_POLL_SCOPES:
        raise MatchbookMarketReadError("poll universe must be non-empty and bounded")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in scope_sha256s:
        if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise MatchbookMarketReadError("poll scope identity must be lowercase SHA-256")
        if value in seen:
            raise MatchbookMarketReadError("poll universe contains duplicate scope identity")
        seen.add(value)
        normalized.append(value)
    normalized.sort()
    if type(request_budget) is not int or isinstance(request_budget, bool) or request_budget <= 0:
        raise MatchbookMarketReadError("request_budget must be a positive integer")
    universe_sha = _digest({"v": 1, "scopes": normalized})

    if checkpoint is None:
        start = 0
        cycle = 0
    else:
        if type(checkpoint) is not MatchbookPollCheckpoint:
            raise MatchbookMarketReadError("checkpoint is invalid")
        if checkpoint.universe_sha256 != universe_sha:
            raise MatchbookMarketReadError("poll checkpoint does not match current universe")
        if checkpoint.next_index >= len(normalized):
            raise MatchbookMarketReadError("poll checkpoint next_index is outside universe")
        start = checkpoint.next_index
        cycle = checkpoint.cycle

    count = min(request_budget, len(normalized))
    chosen = tuple(normalized[(start + offset) % len(normalized)] for offset in range(count))
    raw_next = start + count
    next_index = raw_next % len(normalized)
    next_cycle = cycle + raw_next // len(normalized)
    next_checkpoint = MatchbookPollCheckpoint(universe_sha, next_index, next_cycle)
    return MatchbookPollPlan(chosen, next_checkpoint, request_budget, len(normalized))


__all__ = [
    "MatchbookEnvironment",
    "MatchbookExchangeType",
    "MatchbookMarketReadError",
    "MatchbookMarketReadScope",
    "MatchbookMarketSnapshot",
    "MatchbookMarketState",
    "MatchbookOddsType",
    "MatchbookPollCheckpoint",
    "MatchbookPollPlan",
    "MatchbookPriceLevel",
    "MatchbookPriceMode",
    "MatchbookSide",
    "compare_displayed_liquidity",
    "parse_matchbook_price_snapshot",
    "plan_matchbook_poll_batch",
    "require_current_open_snapshot",
]
