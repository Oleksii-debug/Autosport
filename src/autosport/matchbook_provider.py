from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .domain import MarketType, utc_now_iso
from .providers import ProviderBatch, ProviderQuote, ProviderUnavailableError


_MATCHBOOK_BASE_URL = "https://api.matchbook.com"
_MATCHBOOK_EVENTS_PATH = "/edge/rest/events"
_ALLOWED_PRICE_MODES = frozenset({"expanded", "aggregated"})
_ALLOWED_EVENT_STATES = frozenset({"open", "suspended", "closed", "graded"})
_ALLOWED_RESPONSE_STATES = frozenset(
    {"open", "suspended", "closed", "graded", "withdrawn"}
)
_ALLOWED_CURRENCIES = frozenset({"AUD", "CAD", "EUR", "GBP", "HKD", "USD"})
_MAX_ATTEMPTS = 5
_MAX_PER_PAGE = 100
_SIGNED_64_MAX = (1 << 63) - 1
_HEX = frozenset("0123456789abcdef")
_FROZEN_REQUEST_CONFIGURATION_FIELDS = frozenset(
    {
        "sport_key",
        "currency",
        "sport_ids",
        "event_ids",
        "states",
        "price_mode",
        "price_depth",
        "minimum_liquidity",
        "offset",
        "per_page",
        "_minimum_liquidity_query",
        "market_view_sha256",
        "source_id",
        "request_query_sha256",
        "_sealed_request_configuration_state",
        "_request_configuration_sealed",
        "transport",
        "_provider_origin_verified",
    }
)


class MatchbookPayloadError(ValueError):
    """Provider response violates the bounded read-only Matchbook contract."""


class MatchbookTransportError(ProviderUnavailableError):
    """Recoverable Matchbook read unavailability without leaking credentials."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class MatchbookSequenceAuthorityError(RuntimeError):
    """Product-owned durable acquisition ordering is unavailable or incoherent."""


@dataclass(frozen=True, slots=True)
class MatchbookHttpJsonResponse:
    payload: Any
    status_code: int
    headers: Mapping[str, str]
    body_sha256: str | None = None
    body_size_bytes: int | None = None


Transport = Callable[[str, Mapping[str, str], float], MatchbookHttpJsonResponse]
Clock = Callable[[], str]
Sleeper = Callable[[float], None]
SequenceAllocator = Callable[[str], int]


def _decode_provider_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MatchbookPayloadError("provider returned invalid UTF-8 JSON") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise MatchbookPayloadError(f"provider returned duplicate JSON key {key!r}")
            payload[key] = value
        return payload

    def reject_non_finite(value: str) -> None:
        raise MatchbookPayloadError(
            f"provider returned non-standard JSON constant {value!r}"
        )

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise MatchbookPayloadError("provider returned invalid JSON") from exc


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _default_transport(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> MatchbookHttpJsonResponse:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed HTTPS host/path
            raw = response.read()
            return MatchbookHttpJsonResponse(
                payload=_decode_provider_json(raw),
                status_code=int(response.status),
                headers=dict(response.headers.items()),
                body_sha256=hashlib.sha256(raw).hexdigest(),
                body_size_bytes=len(raw),
            )
    except HTTPError as exc:
        retry_after = _parse_retry_after(
            exc.headers.get("Retry-After") if exc.headers is not None else None
        )
        raise MatchbookTransportError(
            f"Matchbook HTTP {int(exc.code)}",
            status_code=int(exc.code),
            retry_after=retry_after,
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise MatchbookTransportError("Matchbook transport unavailable") from exc


def _positive_int(value: object, *, field: str, maximum: int | None = None) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive non-boolean integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum}")
    return value


def _nonnegative_int(value: object, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative non-boolean integer")
    return value


def _runtime_float(value: object, *, field: str, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    if positive and result <= 0:
        raise ValueError(f"{field} must be positive")
    if not positive and result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _session_token(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("session_token must be a non-empty trimmed string")
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError("session_token contains forbidden control characters")
    return value


def _observation_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MatchbookPayloadError(
            "observation clock must return a non-empty trimmed ISO-8601 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatchbookPayloadError(
            "observation clock must return a valid ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MatchbookPayloadError(
            "observation clock timestamp must be timezone-aware"
        )
    return value


def _optional_body_size(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise MatchbookPayloadError(
            "response body_size_bytes must be a positive non-boolean integer"
        )
    return value


def _sport_key(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("sport_key must be a non-empty trimmed string")
    if value != value.lower() or any(
        ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in value
    ):
        raise ValueError("sport_key must be a lowercase canonical identity")
    return value


def _native_id(value: object, *, field: str) -> str:
    parsed: int
    if type(value) is int:
        parsed = value
    elif isinstance(value, str):
        if (
            not value
            or any(character not in "0123456789" for character in value)
            or (len(value) > 1 and value[0] == "0")
        ):
            raise MatchbookPayloadError(
                f"{field} must be a positive canonical signed-64 provider integer "
                "or decimal string"
            )
        parsed = int(value)
    else:
        raise MatchbookPayloadError(
            f"{field} must be a positive canonical signed-64 provider integer "
            "or decimal string"
        )
    if parsed <= 0 or parsed > _SIGNED_64_MAX:
        raise MatchbookPayloadError(
            f"{field} must be a positive canonical signed-64 provider integer "
            "or decimal string"
        )
    return str(parsed)


def _native_id_matches(value: object, expected: str, *, field: str) -> None:
    actual = _native_id(value, field=field)
    if actual != expected:
        raise MatchbookPayloadError(f"{field} does not match its parent identity")


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MatchbookPayloadError(f"{field} must be a non-empty trimmed string")
    return value


def _status(value: object, *, field: str) -> str:
    result = _text(value, field=field)
    if result not in _ALLOWED_RESPONSE_STATES:
        raise MatchbookPayloadError(f"{field} has unsupported state {result!r}")
    return result


def _event_status(value: object, *, field: str) -> str:
    result = _status(value, field=field)
    if result not in _ALLOWED_EVENT_STATES:
        raise MatchbookPayloadError(f"{field} has unsupported event state {result!r}")
    return result


def _effective_status(event_status: str, market_status: str, runner_status: str) -> str:
    for value in (runner_status, market_status, event_status):
        if value != "open":
            return value
    return "open"


def _decimal_number(
    value: object,
    *,
    field: str,
    greater_than_one: bool = False,
) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (int, Decimal)):
        raise MatchbookPayloadError(
            f"{field} must come from an exact JSON integer/decimal number"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MatchbookPayloadError(f"{field} is not a valid decimal number") from exc
    if not result.is_finite():
        raise MatchbookPayloadError(f"{field} must be finite")
    if greater_than_one:
        if result <= 1:
            raise MatchbookPayloadError(f"{field} must be greater than 1")
    elif result < 0:
        raise MatchbookPayloadError(f"{field} must be non-negative")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _semantic_decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _currency(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("currency must be a supported Matchbook currency string")
    if value not in _ALLOWED_CURRENCIES:
        allowed = ", ".join(sorted(_ALLOWED_CURRENCIES))
        raise ValueError(f"currency must be one of {allowed}")
    return value


def _stable_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _market_view_sha256(
    *,
    currency: str,
    price_mode: str,
    minimum_liquidity: Decimal,
) -> str:
    return _stable_sha256(
        {
            "schema_version": 1,
            "endpoint_family": "events",
            "exchange_type": "back-lay",
            "odds_type": "DECIMAL",
            "include_prices": True,
            "price_depth": 1,
            "price_mode": price_mode,
            "side_filter": "both",
            "currency": currency,
            "minimum_liquidity": _semantic_decimal_text(minimum_liquidity),
            "exclude_mirrored_prices": False,
        }
    )


def _query_sha256(query: list[tuple[str, str]]) -> str:
    return _stable_sha256(query)


def _optional_sha256(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(ch not in _HEX for ch in value)
    ):
        raise MatchbookPayloadError("response body_sha256 must be lowercase SHA-256 hex")
    return value


def _market_type(value: str) -> MarketType:
    normalized = value.lower().replace("-", "_").replace(" ", "_")
    if normalized in {"winner", "moneyline", "money_line", "match_odds", "match_winner"}:
        return MarketType.WINNER
    if "handicap" in normalized or "spread" in normalized:
        return MarketType.HANDICAP
    if "total" in normalized or "over_under" in normalized:
        return MarketType.TOTAL
    return MarketType.OTHER


class MatchbookReadOnlyProvider:
    """Authenticated Matchbook GET-only top-of-book market adapter.

    The generic Autosport quote contract has one canonical price per
    event/market/runner/side identity. This adapter therefore fixes Matchbook
    price-depth to 1 instead of fabricating a depth dimension inside native
    runner identity. It never logs or persists the session token and exposes no
    betting-write operation.
    """

    def __setattr__(self, name: str, value: object) -> None:
        if (
            getattr(self, "_request_configuration_sealed", False)
            and name in _FROZEN_REQUEST_CONFIGURATION_FIELDS
        ):
            raise AttributeError(
                "Matchbook request configuration is immutable after construction"
            )
        object.__setattr__(self, name, value)

    def __init__(
        self,
        session_token: str,
        *,
        sport_key: str,
        currency: str,
        sport_ids: tuple[int, ...] = (),
        event_ids: tuple[int, ...] = (),
        states: tuple[str, ...] = ("open", "suspended"),
        price_mode: str = "expanded",
        price_depth: int = 1,
        minimum_liquidity: Decimal = Decimal("0"),
        offset: int = 0,
        per_page: int = 20,
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
        max_backoff_seconds: float = 2.0,
        transport: Transport = _default_transport,
        clock: Clock = utc_now_iso,
        sleeper: Sleeper = time.sleep,
        sequence_allocator: SequenceAllocator | None = None,
    ) -> None:
        self._session_token = _session_token(session_token)
        self.sport_key = _sport_key(sport_key)
        self.currency = _currency(currency)
        self.sport_ids = self._id_filter(sport_ids, field="sport_ids")
        self.event_ids = self._id_filter(event_ids, field="event_ids")
        if not self.sport_ids and not self.event_ids:
            raise ValueError("at least one sport_ids or event_ids filter is required")
        if type(states) is not tuple or not states:
            raise ValueError("states must be a non-empty tuple")
        normalized_states: list[str] = []
        for value in states:
            if not isinstance(value, str):
                raise ValueError("states must contain strings")
            state = value.strip().lower()
            if state not in _ALLOWED_EVENT_STATES or state != value:
                raise ValueError("states must contain canonical Matchbook event states")
            normalized_states.append(state)
        if len(set(normalized_states)) != len(normalized_states):
            raise ValueError("states must not contain duplicates")
        self.states = tuple(normalized_states)
        if price_mode not in _ALLOWED_PRICE_MODES:
            raise ValueError("price_mode must be 'expanded' or 'aggregated'")
        self.price_mode = price_mode
        if type(price_depth) is not int or price_depth != 1:
            raise ValueError(
                "price_depth must be 1 until canonical ladder-depth identity exists"
            )
        self.price_depth = price_depth
        if not isinstance(minimum_liquidity, Decimal):
            raise TypeError("minimum_liquidity must be Decimal")
        if not minimum_liquidity.is_finite() or minimum_liquidity < 0:
            raise ValueError("minimum_liquidity must be finite and non-negative")
        self.minimum_liquidity = minimum_liquidity
        self._minimum_liquidity_query = _decimal_text(self.minimum_liquidity)
        self.market_view_sha256 = _market_view_sha256(
            currency=self.currency,
            price_mode=self.price_mode,
            minimum_liquidity=self.minimum_liquidity,
        )
        self.offset = _nonnegative_int(offset, field="offset")
        self.per_page = _positive_int(per_page, field="per_page", maximum=_MAX_PER_PAGE)
        self.timeout_seconds = _runtime_float(
            timeout_seconds, field="timeout_seconds", positive=True
        )
        self.max_attempts = _positive_int(
            max_attempts, field="max_attempts", maximum=_MAX_ATTEMPTS
        )
        self.max_backoff_seconds = _runtime_float(
            max_backoff_seconds, field="max_backoff_seconds", positive=False
        )
        self.transport = transport
        self._provider_origin_verified = transport is _default_transport
        self.clock = clock
        self.sleeper = sleeper
        if sequence_allocator is None or not callable(sequence_allocator):
            raise ValueError(
                "sequence_allocator must be an explicit product-owned durable callable"
            )
        self._sequence_allocator = sequence_allocator
        self._last_sequence: int | None = None
        self.source_id = (
            f"matchbook:{self.sport_key}:{self.currency}:{self.price_mode}:"
            f"{self.market_view_sha256}"
        )
        self.request_query_sha256 = _query_sha256(self._query_pairs())
        self._pending_quotes: tuple[ProviderQuote, ...] | None = None
        self._pending_offset = 0
        self._pending_cursor: str | None = None
        self._pending_flags: tuple[str, ...] = ()
        self._sealed_request_configuration_state = self._request_configuration_state()
        self._request_configuration_sealed = True

    def _request_configuration_state(self) -> tuple[object, ...]:
        minimum_liquidity = self.minimum_liquidity
        minimum_liquidity_state: object
        if type(minimum_liquidity) is Decimal:
            minimum_liquidity_state = minimum_liquidity.as_tuple()
        else:
            minimum_liquidity_state = minimum_liquidity
        return (
            (type(self.sport_key), self.sport_key),
            (type(self.currency), self.currency),
            (type(self.sport_ids), self.sport_ids),
            (type(self.event_ids), self.event_ids),
            (type(self.states), self.states),
            (type(self.price_mode), self.price_mode),
            (type(self.price_depth), self.price_depth),
            (type(minimum_liquidity), minimum_liquidity_state),
            (type(self.offset), self.offset),
            (type(self.per_page), self.per_page),
            (type(self._minimum_liquidity_query), self._minimum_liquidity_query),
            (type(self.market_view_sha256), self.market_view_sha256),
            (type(self.source_id), self.source_id),
            (type(self.request_query_sha256), self.request_query_sha256),
            (type(self._provider_origin_verified), self._provider_origin_verified),
            (self.transport is _default_transport, id(self.transport)),
        )

    def _assert_request_configuration_unchanged(self) -> None:
        if self._request_configuration_state() != self._sealed_request_configuration_state:
            raise MatchbookPayloadError(
                "Matchbook request configuration changed after construction"
            )

    @staticmethod
    def _id_filter(values: object, *, field: str) -> tuple[int, ...]:
        if type(values) is not tuple:
            raise TypeError(f"{field} must be a tuple of positive integers")
        output: list[int] = []
        for value in values:
            output.append(_positive_int(value, field=field))
        if len(set(output)) != len(output):
            raise ValueError(f"{field} must not contain duplicates")
        return tuple(output)

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        self._assert_request_configuration_unchanged()
        max_items = _positive_int(max_items, field="max_items")
        if self._pending_quotes is None:
            response = self._request(self._url())
            self._assert_request_configuration_unchanged()
            observed_ts = _observation_timestamp(self.clock())
            body_sha256 = _optional_sha256(response.body_sha256)
            body_size_bytes = _optional_body_size(response.body_size_bytes)
            provider_origin_verified = self.transport is _default_transport
            if provider_origin_verified != self._provider_origin_verified:
                raise MatchbookPayloadError(
                    "Matchbook provider-origin transport identity changed after construction"
                )
            if provider_origin_verified and (
                body_sha256 is None or body_size_bytes is None
            ):
                raise MatchbookPayloadError(
                    "fixed-origin transport must bind raw response digest and byte count"
                )
            sequence = self._allocate_sequence()
            quotes = self._materialize_quotes(
                response.payload,
                observed_ts=observed_ts,
                sequence=sequence,
                http_status=response.status_code,
                body_sha256=body_sha256,
                body_size_bytes=body_size_bytes,
                provider_origin_verified=provider_origin_verified,
            )
            # Do not publish a materialized snapshot if origin-critical transport
            # state changed while the response was being timed, sequenced, or parsed.
            self._assert_request_configuration_unchanged()
            self._pending_quotes = quotes
            self._pending_offset = 0
            self._pending_cursor = body_sha256 or observed_ts
            flags = ["MATCHBOOK_PAGE_SCOPE_ONLY"]
            if self.price_mode == "aggregated":
                flags.append("MATCHBOOK_AGGREGATED_PRICE_MODE")
            self._pending_flags = tuple(flags)

        assert self._pending_quotes is not None
        start = self._pending_offset
        end = min(start + max_items, len(self._pending_quotes))
        quotes = self._pending_quotes[start:end]
        self._pending_offset = end
        cursor = self._pending_cursor
        flags = self._pending_flags
        if end < len(self._pending_quotes):
            flags = flags + ("TRUNCATED_BATCH",)
        else:
            self._clear_pending_snapshot()
        return ProviderBatch(self.source_id, tuple(quotes), cursor=cursor, quality_flags=flags)

    def _allocate_sequence(self) -> int:
        try:
            value = self._sequence_allocator(self.source_id)
        except Exception as exc:
            raise MatchbookSequenceAuthorityError(
                "product sequence authority failed to allocate"
            ) from exc
        if type(value) is not int or value <= 0 or value > _SIGNED_64_MAX:
            raise MatchbookSequenceAuthorityError(
                "product sequence authority must return a positive signed-64 integer"
            )
        if self._last_sequence is not None and value <= self._last_sequence:
            raise MatchbookSequenceAuthorityError(
                "product sequence authority must strictly advance"
            )
        self._last_sequence = value
        return value

    def _clear_pending_snapshot(self) -> None:
        self._pending_quotes = None
        self._pending_offset = 0
        self._pending_cursor = None
        self._pending_flags = ()

    def _query_pairs(self) -> list[tuple[str, str]]:
        query: list[tuple[str, str]] = [
            ("offset", str(self.offset)),
            ("per-page", str(self.per_page)),
            ("states", ",".join(self.states)),
            ("exchange-type", "back-lay"),
            ("odds-type", "DECIMAL"),
            ("include-prices", "true"),
            ("price-depth", "1"),
            ("price-mode", self.price_mode),
            ("currency", self.currency),
            ("minimum-liquidity", self._minimum_liquidity_query),
            ("exclude-mirrored-prices", "false"),
        ]
        if self.sport_ids:
            query.append(("sport-ids", ",".join(str(value) for value in self.sport_ids)))
        if self.event_ids:
            query.append(("ids", ",".join(str(value) for value in self.event_ids)))
        return query

    def _url(self) -> str:
        return (
            f"{_MATCHBOOK_BASE_URL}{_MATCHBOOK_EVENTS_PATH}?"
            f"{urlencode(self._query_pairs())}"
        )

    def _request(self, url: str) -> MatchbookHttpJsonResponse:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Autosport/0.1 read-only-market-observer",
            "session-token": self._session_token,
        }
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.transport(url, headers, self.timeout_seconds)
                if type(response.status_code) is not int:
                    raise MatchbookPayloadError(
                        "transport response status_code must be a non-boolean integer"
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise MatchbookTransportError(
                        f"Matchbook HTTP {response.status_code}",
                        status_code=response.status_code,
                    )
                return response
            except MatchbookTransportError as exc:
                retryable = exc.status_code == 429 or (
                    exc.status_code is not None and exc.status_code >= 500
                )
                if not retryable or attempt >= self.max_attempts:
                    raise
                requested = (
                    exc.retry_after if exc.retry_after is not None else 0.25 * attempt
                )
                if (
                    isinstance(requested, bool)
                    or not isinstance(requested, (int, float))
                    or not math.isfinite(float(requested))
                    or float(requested) < 0
                ):
                    requested = 0.25 * attempt
                self.sleeper(
                    min(float(requested), self.max_backoff_seconds)
                )
        raise AssertionError("unreachable")

    def _materialize_quotes(
        self,
        payload: object,
        *,
        observed_ts: str,
        sequence: int,
        http_status: int,
        body_sha256: str | None,
        body_size_bytes: int | None,
        provider_origin_verified: bool,
    ) -> tuple[ProviderQuote, ...]:
        if not isinstance(payload, dict):
            raise MatchbookPayloadError("Matchbook events response must be an object")
        events = payload.get("events")
        if not isinstance(events, list):
            raise MatchbookPayloadError("Matchbook events response requires events[]")
        output: list[ProviderQuote] = []
        seen: set[tuple[str, str, str, str]] = set()
        for event in events:
            if not isinstance(event, dict):
                raise MatchbookPayloadError("event entries must be objects")
            event_id = _native_id(event.get("id"), field="event.id")
            event_status = _event_status(event.get("status"), field="event.status")
            markets = event.get("markets")
            if not isinstance(markets, list):
                raise MatchbookPayloadError("event.markets must be a list")
            for market in markets:
                if not isinstance(market, dict):
                    raise MatchbookPayloadError("market entries must be objects")
                market_id = _native_id(market.get("id"), field="market.id")
                _native_id_matches(
                    market.get("event-id"), event_id, field="market.event-id"
                )
                market_status = _status(market.get("status"), field="market.status")
                market_type_raw = _text(
                    market.get("market-type"), field="market.market-type"
                )
                runners = market.get("runners")
                if not isinstance(runners, list):
                    raise MatchbookPayloadError("market.runners must be a list")
                for runner in runners:
                    if not isinstance(runner, dict):
                        raise MatchbookPayloadError("runner entries must be objects")
                    runner_id = _native_id(runner.get("id"), field="runner.id")
                    _native_id_matches(
                        runner.get("event-id"), event_id, field="runner.event-id"
                    )
                    _native_id_matches(
                        runner.get("market-id"), market_id, field="runner.market-id"
                    )
                    runner_status = _status(
                        runner.get("status"), field="runner.status"
                    )
                    prices = runner.get("prices")
                    if not isinstance(prices, list):
                        raise MatchbookPayloadError("runner.prices must be a list")
                    for price in prices:
                        if not isinstance(price, dict):
                            raise MatchbookPayloadError("price entries must be objects")
                        side = _text(price.get("side"), field="price.side")
                        if side not in {"back", "lay"}:
                            raise MatchbookPayloadError(
                                "price.side must be canonical 'back' or 'lay'"
                            )
                        if _text(
                            price.get("exchange-type"), field="price.exchange-type"
                        ) != "back-lay":
                            raise MatchbookPayloadError(
                                "price.exchange-type must be 'back-lay'"
                            )
                        if _text(
                            price.get("odds-type"), field="price.odds-type"
                        ) != "DECIMAL":
                            raise MatchbookPayloadError(
                                "price.odds-type must be 'DECIMAL'"
                            )
                        odds = _decimal_number(
                            price.get("decimal-odds"),
                            field="price.decimal-odds",
                            greater_than_one=True,
                        )
                        available_amount = _decimal_number(
                            price.get("available-amount"),
                            field="price.available-amount",
                        )
                        currency = _text(price.get("currency"), field="price.currency")
                        if currency != self.currency:
                            raise MatchbookPayloadError(
                                "price.currency does not match requested currency"
                            )
                        identity = (event_id, market_id, runner_id, side)
                        if identity in seen:
                            raise MatchbookPayloadError(
                                "duplicate native event/market/runner/side price identity"
                            )
                        seen.add(identity)
                        metadata: dict[str, Any] = {
                            "provider": "matchbook",
                            "provider_origin_verified": provider_origin_verified,
                            "parsed_payload_bound_to_raw_response": (
                                provider_origin_verified
                                and body_sha256 is not None
                                and body_size_bytes is not None
                            ),
                            "native_event_id": event_id,
                            "native_market_id": market_id,
                            "native_runner_id": runner_id,
                            "available_amount": _decimal_text(available_amount),
                            "price_mode": self.price_mode,
                            "depth_level": 0,
                            "requested_price_depth": 1,
                            "exchange_type": "back-lay",
                            "odds_type": "DECIMAL",
                            "currency": currency,
                            "requested_currency": self.currency,
                            "minimum_liquidity": self._minimum_liquidity_query,
                            "side_filter": "both",
                            "exclude_mirrored_prices": False,
                            "market_view_sha256": self.market_view_sha256,
                            "request_query_sha256": self.request_query_sha256,
                            "event_status": event_status,
                            "market_status": market_status,
                            "runner_status": runner_status,
                            "market_type": market_type_raw,
                            "http_status": int(http_status),
                            "endpoint": _MATCHBOOK_EVENTS_PATH,
                            "page_offset": self.offset,
                            "page_size": self.per_page,
                            "page_scope_complete": False,
                        }
                        if body_sha256 is not None:
                            metadata["response_sha256"] = body_sha256
                        if body_size_bytes is not None:
                            metadata["response_size_bytes"] = body_size_bytes
                        output.append(
                            ProviderQuote(
                                provider_event_id=event_id,
                                provider_market_id=market_id,
                                provider_selection_id=runner_id,
                                decimal_odds=odds,
                                observed_ts=observed_ts,
                                sequence=sequence,
                                market_type=_market_type(market_type_raw),
                                status=_effective_status(
                                    event_status, market_status, runner_status
                                ),
                                source_ts=None,
                                metadata=metadata,
                                sport=self.sport_key,
                                exchange_side=side,
                            )
                        )
        return tuple(output)
