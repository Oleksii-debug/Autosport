from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .domain import MarketType, utc_now_iso
from .providers import ProviderBatch, ProviderQuote, ProviderUnavailableError


_BASE_URL = "https://api.the-odds-api.com"
_TERMS_VERSION = "2026-08-31"
_HEX = frozenset("0123456789abcdef")
_COMPONENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
_QUOTA_HEADERS = (
    "x-requests-remaining",
    "x-requests-used",
    "x-requests-last",
)


class TheOddsApiPayloadError(ValueError):
    """The Odds API response violates the bounded reference-data contract."""


class TheOddsApiTransportError(ProviderUnavailableError):
    """Read-only reference transport is unavailable without leaking credentials."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class TheOddsApiHttpJsonResponse:
    payload: Any
    status_code: int
    headers: Mapping[str, str]
    body_sha256: str | None = None


Transport = Callable[
    [str, Mapping[str, str], str, float],
    TheOddsApiHttpJsonResponse,
]
Clock = Callable[[], str]


def _decode_provider_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TheOddsApiPayloadError("provider returned invalid UTF-8 JSON") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise TheOddsApiPayloadError(
                    f"provider returned duplicate JSON key {key!r}"
                )
            payload[key] = value
        return payload

    def reject_non_finite(value: str) -> None:
        raise TheOddsApiPayloadError(
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
        raise TheOddsApiPayloadError("provider returned invalid JSON") from exc


def _default_transport(
    path: str,
    params: Mapping[str, str],
    api_key: str,
    timeout_seconds: float,
) -> TheOddsApiHttpJsonResponse:
    query = dict(params)
    query["apiKey"] = api_key
    url = f"{_BASE_URL}{path}?{urlencode(query)}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Autosport/0.1 read-only-reference-observer",
        },
        method="GET",
    )
    http_status: int | None = None
    transport_failed = False
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - fixed HTTPS host/path
            raw = response.read()
            return TheOddsApiHttpJsonResponse(
                payload=_decode_provider_json(raw),
                status_code=int(response.status),
                headers=dict(response.headers.items()),
                body_sha256=hashlib.sha256(raw).hexdigest(),
            )
    except HTTPError as exc:
        http_status = int(exc.code)
    except (URLError, TimeoutError, OSError):
        transport_failed = True

    if http_status is not None:
        raise TheOddsApiTransportError(
            f"The Odds API HTTP {http_status}",
            status_code=http_status,
        )
    if transport_failed:
        raise TheOddsApiTransportError("The Odds API transport unavailable")
    raise AssertionError("unreachable transport outcome")


def _api_key(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("api_key must be a non-empty trimmed string")
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError("api_key contains forbidden control characters")
    return value


def _component(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty canonical string")
    if (
        value != value.lower()
        or not value.isascii()
        or any(ch not in _COMPONENT_CHARS for ch in value)
    ):
        raise ValueError(
            f"{field} must use lowercase ASCII letters/digits joined by '-' or '_'"
        )
    if value in {"unknown", "mixed"}:
        raise ValueError(f"{field} uses a reserved identity")
    return value


def _component_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise ValueError(f"{field} must be a non-empty tuple")
    output = tuple(_component(item, field=field) for item in value)
    if len(set(output)) != len(output):
        raise ValueError(f"{field} must not contain duplicates")
    return output


def _optional_component_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field} must be a tuple")
    if not value:
        return ()
    return _component_tuple(value, field=field)


def _text(value: object, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise TheOddsApiPayloadError(f"{field} must be non-empty trimmed text")
    if "|" in value or "\x00" in value:
        raise TheOddsApiPayloadError(f"{field} contains a reserved character")
    return value


def _timestamp(value: object, *, field: str) -> str:
    raw = _text(value, field=field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TheOddsApiPayloadError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TheOddsApiPayloadError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_timestamp(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _timestamp(value, field=field)


def _timestamp_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _sequence_from_timestamp(value: str) -> int:
    parsed = _timestamp_instant(_timestamp(value, field="acquired_at"))
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed - epoch
    micros = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    if micros < 0 or micros > (1 << 63) - 1:
        raise TheOddsApiPayloadError("acquired_at is outside sequence range")
    return micros


def _decimal_number(
    value: object,
    *,
    field: str,
    greater_than_one: bool = False,
) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TheOddsApiPayloadError(
            f"{field} must come from an exact JSON integer/decimal number"
        )
    if not isinstance(value, (int, Decimal)):
        raise TheOddsApiPayloadError(
            f"{field} must come from an exact JSON integer/decimal number"
        )
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TheOddsApiPayloadError(f"{field} is not a valid decimal number") from exc
    if not result.is_finite():
        raise TheOddsApiPayloadError(f"{field} must be finite")
    if greater_than_one and result <= 1:
        raise TheOddsApiPayloadError(f"{field} must be greater than 1")
    return result


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _optional_sha256(value: object) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(ch not in _HEX for ch in value)
    ):
        raise TheOddsApiPayloadError(
            "response body_sha256 must be lowercase SHA-256 hex"
        )
    return value


def _quota_metadata(headers: Mapping[str, str]) -> dict[str, str]:
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    output: dict[str, str] = {}
    for key in _QUOTA_HEADERS:
        value = lowered.get(key)
        if value is None:
            continue
        if not value or value != value.strip() or any(ch in value for ch in "\r\n\x00"):
            raise TheOddsApiPayloadError(f"invalid quota header {key}")
        output[key] = value
    return output


def _market_type(market_key: str) -> MarketType:
    if market_key == "h2h":
        return MarketType.WINNER
    if market_key == "spreads":
        return MarketType.HANDICAP
    if market_key == "totals":
        return MarketType.TOTAL
    return MarketType.OTHER


class TheOddsApiReferenceProvider:
    """Read-only multi-sport reference adapter for The Odds API v4.

    This adapter emits reference observations only. It deliberately does not
    expose execution methods, accepted-price authority, fair-value authority, or
    a raw-data redistribution surface. Historical provider timestamps never
    replace Autosport acquisition time: a historical response fetched later
    remains later-acquired evidence for causal decision-time use.
    """

    def __init__(
        self,
        api_key: str,
        *,
        sport_key: str,
        markets: tuple[str, ...],
        regions: tuple[str, ...] = (),
        bookmakers: tuple[str, ...] = (),
        historical_at: str | None = None,
        timeout_seconds: float = 10.0,
        transport: Transport = _default_transport,
        clock: Clock = utc_now_iso,
    ) -> None:
        self._api_key = _api_key(api_key)
        self.sport_key = _component(sport_key, field="sport_key")
        self.markets = _component_tuple(markets, field="markets")
        self.regions = _optional_component_tuple(regions, field="regions")
        self.bookmakers = _optional_component_tuple(bookmakers, field="bookmakers")
        if bool(self.regions) == bool(self.bookmakers):
            raise ValueError("exactly one of regions or bookmakers must be configured")
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        timeout = float(timeout_seconds)
        if not (timeout > 0 and timeout < float("inf")):
            raise ValueError("timeout_seconds must be a positive finite number")
        self.timeout_seconds = timeout
        self.transport = transport
        self.clock = clock
        self.historical_at = (
            None
            if historical_at is None
            else _timestamp(historical_at, field="historical_at")
        )
        mode = "historical" if self.historical_at is not None else "current"
        self.source_id = f"the-odds-api:{self.sport_key}:{mode}"
        self._pending_quotes: tuple[ProviderQuote, ...] | None = None
        self._pending_offset = 0
        self._pending_cursor: str | None = None
        self._pending_flags: tuple[str, ...] = ()

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be a positive non-boolean integer")
        if self._pending_quotes is None:
            acquired_at = _timestamp(self.clock(), field="acquired_at")
            sequence = _sequence_from_timestamp(acquired_at)
            path, params = self._request_scope()
            response = self._request(path, params)
            body_sha256 = _optional_sha256(response.body_sha256)
            quotes, flags = self._materialize_response(
                response,
                acquired_at=acquired_at,
                sequence=sequence,
                endpoint=path,
                request_params=params,
                body_sha256=body_sha256,
            )
            self._pending_quotes = quotes
            self._pending_offset = 0
            self._pending_cursor = body_sha256 or acquired_at
            self._pending_flags = flags

        assert self._pending_quotes is not None
        start = self._pending_offset
        end = min(start + max_items, len(self._pending_quotes))
        quotes = self._pending_quotes[start:end]
        flags = self._pending_flags
        cursor = self._pending_cursor
        self._pending_offset = end
        if end < len(self._pending_quotes):
            flags = flags + ("TRUNCATED_BATCH",)
        else:
            self._clear_pending_snapshot()
        return ProviderBatch(self.source_id, tuple(quotes), cursor=cursor, quality_flags=flags)

    def _clear_pending_snapshot(self) -> None:
        self._pending_quotes = None
        self._pending_offset = 0
        self._pending_cursor = None
        self._pending_flags = ()

    def _request_scope(self) -> tuple[str, dict[str, str]]:
        if self.historical_at is None:
            path = f"/v4/sports/{self.sport_key}/odds"
        else:
            path = f"/v4/historical/sports/{self.sport_key}/odds"
        params = {
            "markets": ",".join(self.markets),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        if self.regions:
            params["regions"] = ",".join(self.regions)
        else:
            params["bookmakers"] = ",".join(self.bookmakers)
        if self.historical_at is not None:
            params["date"] = self.historical_at
        return path, params

    def _request(
        self,
        path: str,
        params: Mapping[str, str],
    ) -> TheOddsApiHttpJsonResponse:
        response = self.transport(
            path,
            params,
            self._api_key,
            self.timeout_seconds,
        )
        if type(response) is not TheOddsApiHttpJsonResponse:
            raise TheOddsApiPayloadError(
                "transport must return exact TheOddsApiHttpJsonResponse"
            )
        if type(response.status_code) is not int:
            raise TheOddsApiPayloadError(
                "transport response status_code must be a non-boolean integer"
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise TheOddsApiTransportError(
                f"The Odds API HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return response

    def _materialize_response(
        self,
        response: TheOddsApiHttpJsonResponse,
        *,
        acquired_at: str,
        sequence: int,
        endpoint: str,
        request_params: Mapping[str, str],
        body_sha256: str | None,
    ) -> tuple[tuple[ProviderQuote, ...], tuple[str, ...]]:
        returned_snapshot: str | None = None
        previous_snapshot: str | None = None
        next_snapshot: str | None = None

        if self.historical_at is None:
            payload = response.payload
            if not isinstance(payload, list):
                raise TheOddsApiPayloadError("current odds response must be a list")
            events = payload
            mode = "current"
        else:
            payload = response.payload
            if not isinstance(payload, dict):
                raise TheOddsApiPayloadError(
                    "historical odds response must be an object"
                )
            if set(("timestamp", "data")) - set(payload):
                raise TheOddsApiPayloadError(
                    "historical odds response requires timestamp and data"
                )
            returned_snapshot = _timestamp(
                payload.get("timestamp"),
                field="historical.timestamp",
            )
            assert self.historical_at is not None
            if _timestamp_instant(returned_snapshot) > _timestamp_instant(
                self.historical_at
            ):
                raise TheOddsApiPayloadError(
                    "historical returned snapshot is after requested cutoff"
                )
            previous_snapshot = _optional_timestamp(
                payload.get("previous_timestamp"),
                field="historical.previous_timestamp",
            )
            next_snapshot = _optional_timestamp(
                payload.get("next_timestamp"),
                field="historical.next_timestamp",
            )
            returned_instant = _timestamp_instant(returned_snapshot)
            if (
                previous_snapshot is not None
                and _timestamp_instant(previous_snapshot) >= returned_instant
            ):
                raise TheOddsApiPayloadError(
                    "historical previous snapshot must precede returned snapshot"
                )
            if (
                next_snapshot is not None
                and _timestamp_instant(next_snapshot) <= returned_instant
            ):
                raise TheOddsApiPayloadError(
                    "historical next snapshot must follow returned snapshot"
                )
            events = payload.get("data")
            if not isinstance(events, list):
                raise TheOddsApiPayloadError("historical data must be a list")
            mode = "historical"

        quota = _quota_metadata(response.headers)
        output: list[ProviderQuote] = []
        seen_identity: set[tuple[str, str, str]] = set()
        observed_pairs: set[tuple[str, str]] = set()
        observed_markets: set[str] = set()

        for event in events:
            if not isinstance(event, dict):
                raise TheOddsApiPayloadError("event entries must be objects")
            event_id = _text(event.get("id"), field="event.id")
            event_sport = _text(event.get("sport_key"), field="event.sport_key")
            if event_sport != self.sport_key:
                raise TheOddsApiPayloadError(
                    "event.sport_key does not match configured sport"
                )
            commence_time = _timestamp(
                event.get("commence_time"),
                field="event.commence_time",
            )
            bookmakers = event.get("bookmakers")
            if not isinstance(bookmakers, list):
                raise TheOddsApiPayloadError("event.bookmakers must be a list")

            for bookmaker in bookmakers:
                if not isinstance(bookmaker, dict):
                    raise TheOddsApiPayloadError(
                        "bookmaker entries must be objects"
                    )
                bookmaker_key = _component(
                    bookmaker.get("key"),
                    field="bookmaker.key",
                )
                if self.bookmakers and bookmaker_key not in self.bookmakers:
                    raise TheOddsApiPayloadError(
                        "provider returned bookmaker outside requested scope"
                    )
                bookmaker_title = _text(
                    bookmaker.get("title"),
                    field="bookmaker.title",
                )
                bookmaker_last_update = _optional_timestamp(
                    bookmaker.get("last_update"),
                    field="bookmaker.last_update",
                )
                market_rows = bookmaker.get("markets")
                if not isinstance(market_rows, list):
                    raise TheOddsApiPayloadError(
                        "bookmaker.markets must be a list"
                    )

                for market in market_rows:
                    if not isinstance(market, dict):
                        raise TheOddsApiPayloadError(
                            "market entries must be objects"
                        )
                    market_key = _component(
                        market.get("key"),
                        field="market.key",
                    )
                    if market_key not in self.markets:
                        raise TheOddsApiPayloadError(
                            "provider returned market outside requested scope"
                        )
                    market_last_update = _optional_timestamp(
                        market.get("last_update"),
                        field="market.last_update",
                    )
                    source_ts = market_last_update or bookmaker_last_update
                    if source_ts is None:
                        raise TheOddsApiPayloadError(
                            "market requires provider last_update timestamp"
                        )
                    if (
                        returned_snapshot is not None
                        and _timestamp_instant(source_ts)
                        > _timestamp_instant(returned_snapshot)
                    ):
                        raise TheOddsApiPayloadError(
                            "historical constituent update is after returned snapshot"
                        )
                    outcomes = market.get("outcomes")
                    if not isinstance(outcomes, list):
                        raise TheOddsApiPayloadError(
                            "market.outcomes must be a list"
                        )
                    observed_pairs.add((bookmaker_key, market_key))
                    observed_markets.add(market_key)

                    for outcome in outcomes:
                        if not isinstance(outcome, dict):
                            raise TheOddsApiPayloadError(
                                "outcome entries must be objects"
                            )
                        outcome_name = _text(
                            outcome.get("name"),
                            field="outcome.name",
                        )
                        price = _decimal_number(
                            outcome.get("price"),
                            field="outcome.price",
                            greater_than_one=True,
                        )
                        point_raw = outcome.get("point")
                        point: Decimal | None = None
                        if point_raw is not None:
                            point = _decimal_number(
                                point_raw,
                                field="outcome.point",
                            )
                        selection_id = outcome_name
                        if point is not None:
                            selection_id = (
                                f"{outcome_name}@point={_decimal_text(point)}"
                            )
                        market_id = f"{bookmaker_key}:{market_key}"
                        identity = (event_id, market_id, selection_id)
                        if identity in seen_identity:
                            raise TheOddsApiPayloadError(
                                "duplicate event/bookmaker/market/outcome identity"
                            )
                        seen_identity.add(identity)
                        metadata: dict[str, Any] = {
                            "provider": "the_odds_api",
                            "reference_only": True,
                            "execution_authorized": False,
                            "accepted_price_authorized": False,
                            "fair_value_authorized": False,
                            "bookmaker_key": bookmaker_key,
                            "bookmaker_title": bookmaker_title,
                            "market_key": market_key,
                            "outcome_name": outcome_name,
                            "commence_time": commence_time,
                            "bookmaker_last_update": bookmaker_last_update,
                            "market_last_update": market_last_update,
                            "acquired_at": acquired_at,
                            "request_mode": mode,
                            "endpoint": endpoint,
                            "requested_markets": list(self.markets),
                            "requested_regions": list(self.regions),
                            "requested_bookmakers": list(self.bookmakers),
                            "terms_version": _TERMS_VERSION,
                            "raw_redistribution_authorized": False,
                        }
                        if point is not None:
                            metadata["point"] = _decimal_text(point)
                        if returned_snapshot is not None:
                            metadata["historical_requested_at"] = self.historical_at
                            metadata["historical_returned_at"] = returned_snapshot
                            metadata["historical_previous_at"] = previous_snapshot
                            metadata["historical_next_at"] = next_snapshot
                        if body_sha256 is not None:
                            metadata["response_sha256"] = body_sha256
                        metadata.update(quota)
                        output.append(
                            ProviderQuote(
                                provider_event_id=event_id,
                                provider_market_id=market_id,
                                provider_selection_id=selection_id,
                                decimal_odds=price,
                                observed_ts=acquired_at,
                                sequence=sequence,
                                market_type=_market_type(market_key),
                                status="reference",
                                source_ts=source_ts,
                                metadata=metadata,
                                sport=self.sport_key,
                            )
                        )

        flags: list[str] = ["REFERENCE_ONLY", "NON_EXECUTABLE_REFERENCE"]
        if self.historical_at is not None:
            flags.append("HISTORICAL_SNAPSHOT")
        if not output:
            flags.append("EMPTY_REFERENCE_SCOPE")
        else:
            if self.bookmakers:
                expected = {
                    (bookmaker, market)
                    for bookmaker in self.bookmakers
                    for market in self.markets
                }
                if not expected.issubset(observed_pairs):
                    flags.append("PARTIAL_REFERENCE_SCOPE")
            elif not set(self.markets).issubset(observed_markets):
                flags.append("PARTIAL_REFERENCE_SCOPE")
        return tuple(output), tuple(flags)
