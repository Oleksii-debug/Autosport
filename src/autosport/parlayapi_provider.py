from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .domain import MarketType, utc_now_iso
from .providers import ProviderBatch, ProviderQuote, ProviderUnavailableError


class ProviderPayloadError(ValueError):
    pass


class ProviderTransportError(ProviderUnavailableError):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class HttpJsonResponse:
    payload: Any
    status_code: int
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ProviderSourceState:
    source: str
    age_seconds: float | None
    role: str


@dataclass(frozen=True, slots=True)
class ProviderStateSnapshot:
    server_timestamp: int
    sources: tuple[ProviderSourceState, ...]
    truncated: bool = False

    def for_source(self, source: str) -> ProviderSourceState | None:
        for state in self.sources:
            if state.source == source:
                return state
        return None


@dataclass(frozen=True, slots=True)
class HistoricalCoverageSource:
    source: str
    rows: int
    first_date: str
    last_date: str
    priced_rows: int


@dataclass(frozen=True, slots=True)
class HistoricalCoverageReport:
    sport_key: str
    date_from: str
    date_to: str
    historical_window_hours: int
    historical_window_from: str
    observed_at: str
    response_sha256: str
    sources: tuple[HistoricalCoverageSource, ...]
    api_version: str | None = None

    @property
    def total_rows(self) -> int:
        return sum(item.rows for item in self.sources)

    @property
    def total_priced_rows(self) -> int:
        return sum(item.priced_rows for item in self.sources)

    @property
    def has_data(self) -> bool:
        return bool(self.sources)


Transport = Callable[[str, Mapping[str, str], float], HttpJsonResponse]
Clock = Callable[[], str]
Sleeper = Callable[[float], None]


def _finite_runtime_float(value: object, *, field: str, allow_zero: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{field} must be a finite number")
    if allow_zero:
        if numeric < 0:
            raise ValueError(f"{field} must be non-negative")
    elif numeric <= 0:
        raise ValueError(f"{field} must be positive")
    return numeric


def _positive_nonboolean_int(value: object, *, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive non-boolean integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum}")
    return value


def _provider_identity(value: object, *, field: str, allow_colon: bool = True) -> str:
    """Validate raw provider identity without normalizing away type or byte differences."""

    if not isinstance(value, str):
        raise ProviderPayloadError(f"{field} must be a string")
    if not value or value != value.strip():
        raise ProviderPayloadError(f"{field} must be a non-empty trimmed string")
    if "|" in value:
        raise ProviderPayloadError(f"{field} must not contain reserved identity delimiter '|'")
    if not allow_colon and ":" in value:
        raise ProviderPayloadError(f"{field} must not contain reserved source-scope delimiter ':'")
    return value


def _decode_provider_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProviderPayloadError("provider returned invalid UTF-8 JSON") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise ProviderPayloadError(f"provider returned duplicate JSON key {key!r}")
            payload[key] = value
        return payload

    def reject_non_finite(value: str) -> None:
        raise ProviderPayloadError(f"provider returned non-standard JSON constant {value!r}")

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise ProviderPayloadError("provider returned invalid JSON") from exc


def _default_transport(url: str, headers: Mapping[str, str], timeout: float) -> HttpJsonResponse:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed HTTPS base URL by default
            raw = response.read()
            payload = _decode_provider_json(raw)
            return HttpJsonResponse(payload, int(response.status), dict(response.headers.items()))
    except HTTPError as exc:
        retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
        raise ProviderTransportError(f"provider HTTP {exc.code}", int(exc.code), retry_after) from exc
    except URLError as exc:
        raise ProviderTransportError(f"provider transport error: {exc.reason}") from exc


class ParlayApiTableTennisProvider:
    """Read-only table-tennis odds adapter. No bookmaker account or wager execution capability exists here."""

    source_id = "parlayapi:table_tennis"
    sport_key = "table_tennis"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        public_preview: bool = False,
        regions: tuple[str, ...] = ("us",),
        markets: tuple[str, ...] = ("h2h", "spreads", "totals"),
        base_url: str = "https://parlay-api.com",
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
        max_backoff_seconds: float = 2.0,
        transport: Transport = _default_transport,
        clock: Clock = utc_now_iso,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        if not public_preview and not api_key:
            raise ValueError("api_key is required unless public_preview=True")
        timeout_seconds = _finite_runtime_float(timeout_seconds, field="timeout_seconds", allow_zero=False)
        max_attempts = _positive_nonboolean_int(max_attempts, field="max_attempts", maximum=5)
        max_backoff_seconds = _finite_runtime_float(
            max_backoff_seconds,
            field="max_backoff_seconds",
            allow_zero=True,
        )
        if not regions or not markets:
            raise ValueError("regions and markets must not be empty")
        if not base_url.startswith("https://"):
            raise ValueError("provider base_url must use https")
        self.api_key = api_key
        self.public_preview = public_preview
        self.regions = regions
        self.markets = markets
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_backoff_seconds = max_backoff_seconds
        self.transport = transport
        self.clock = clock
        self.sleeper = sleeper
        self._pending_quotes: Iterator[ProviderQuote] | None = None
        self._pending_quote: ProviderQuote | None = None
        self._pending_cursor: str | None = None
        self._pending_quality_flags: tuple[str, ...] = ()

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        max_items = _positive_nonboolean_int(max_items, field="max_items")
        if self._pending_quotes is None:
            observed_ts = self.clock()
            response = self._fetch()
            events = self._event_list(response.payload)
            provider_state = _parse_provider_state(response.headers)
            self._pending_quality_flags = _provider_state_quality_flags(events, provider_state)
            self._pending_quotes = self._snapshot_quotes(
                events,
                observed_ts,
                response.status_code,
                provider_state,
            )
            self._pending_quote = None
            self._pending_cursor = observed_ts

        cursor = self._pending_cursor
        base_quality_flags = self._pending_quality_flags
        try:
            quotes: list[ProviderQuote] = []
            if self._pending_quote is not None:
                quotes.append(self._pending_quote)
                self._pending_quote = None

            while len(quotes) < max_items:
                try:
                    quotes.append(next(self._pending_quotes))
                except StopIteration:
                    self._clear_pending_snapshot()
                    return ProviderBatch(
                        self.source_id,
                        tuple(quotes),
                        cursor=cursor,
                        quality_flags=base_quality_flags,
                    )

            try:
                self._pending_quote = next(self._pending_quotes)
            except StopIteration:
                self._clear_pending_snapshot()
                quality_flags = base_quality_flags
            else:
                quality_flags = tuple(sorted((*base_quality_flags, "TRUNCATED_BATCH")))
            return ProviderBatch(
                self.source_id,
                tuple(quotes),
                cursor=cursor,
                quality_flags=quality_flags,
            )
        except Exception:
            # The pending iterator may fail only when a deferred event is first
            # materialized. Never leave a closed/poisoned generator installed: a
            # later read must perform a fresh provider acquisition rather than
            # falsely succeeding with an empty batch and the stale cursor.
            self._clear_pending_snapshot()
            raise

    def _snapshot_quotes(
        self,
        events: list[dict[str, Any]],
        observed_ts: str,
        http_status: int,
        provider_state: ProviderStateSnapshot | None,
    ) -> Iterator[ProviderQuote]:
        for event in events:
            yield from self._event_quotes(event, observed_ts, http_status, provider_state)

    def _clear_pending_snapshot(self) -> None:
        self._pending_quotes = None
        self._pending_quote = None
        self._pending_cursor = None
        self._pending_quality_flags = ()

    def historical_coverage(self, date_from: str, date_to: str) -> HistoricalCoverageReport:
        """Verify the authenticated key's requested historical window and actual source coverage.

        The provider documents this endpoint as a one-credit preflight. A successful
        response proves only runtime access/coverage facts for the requested window;
        it does not prove a storage, retention, or redistribution licence.
        """

        if self.public_preview or not self.api_key:
            raise ValueError("historical coverage requires an authenticated API key")
        requested_from = _parse_iso_date(date_from, field="date_from")
        requested_to = _parse_iso_date(date_to, field="date_to")
        if requested_to < requested_from:
            raise ValueError("date_to must not precede date_from")

        query = urlencode({"dateFrom": date_from, "dateTo": date_to})
        url = f"{self.base_url}/v1/historical/sports/{self.sport_key}/coverage?{query}"
        observed_at = self.clock()
        response = self._request(url)
        window_hours_raw = _header(response.headers, "x-historical-window-hours")
        window_from_raw = _header(response.headers, "x-historical-window-from")
        if window_hours_raw is None or window_from_raw is None:
            raise ProviderPayloadError("historical coverage response is missing entitlement-window headers")
        try:
            window_hours = int(window_hours_raw)
        except ValueError as exc:
            raise ProviderPayloadError("x-historical-window-hours must be an integer") from exc
        if window_hours <= 0:
            raise ProviderPayloadError("x-historical-window-hours must be positive")
        entitlement_from = _parse_provider_date(window_from_raw, field="x-historical-window-from")
        if requested_from < entitlement_from:
            raise ProviderPayloadError("historical response contradicts its entitlement-window header")

        payload = response.payload
        if not isinstance(payload, dict):
            raise ProviderPayloadError("historical coverage response must be an object")
        if str(payload.get("sport_key", "")) != self.sport_key:
            raise ProviderPayloadError("historical coverage response sport_key mismatch")
        window = payload.get("window")
        if not isinstance(window, dict):
            raise ProviderPayloadError("historical coverage response requires window object")
        if str(window.get("date_from", "")) != date_from or str(window.get("date_to", "")) != date_to:
            raise ProviderPayloadError("historical coverage response window mismatch")
        by_source = payload.get("by_source")
        if not isinstance(by_source, dict):
            raise ProviderPayloadError("historical coverage response requires by_source object")

        sources: list[HistoricalCoverageSource] = []
        for source, raw in sorted(by_source.items(), key=lambda item: str(item[0])):
            source_name = str(source).strip()
            if not source_name or not isinstance(raw, dict):
                raise ProviderPayloadError("historical coverage source entries must be named objects")
            rows = _nonnegative_int(raw.get("rows"), field=f"by_source.{source_name}.rows")
            priced_rows = _nonnegative_int(
                raw.get("priced_rows"),
                field=f"by_source.{source_name}.priced_rows",
            )
            if rows == 0:
                raise ProviderPayloadError("historical coverage must omit sources with zero rows")
            if priced_rows > rows:
                raise ProviderPayloadError("historical priced_rows must not exceed rows")
            first_date_raw = str(raw.get("first_date", "")).strip()
            last_date_raw = str(raw.get("last_date", "")).strip()
            first_date = _parse_iso_date(first_date_raw, field=f"by_source.{source_name}.first_date")
            last_date = _parse_iso_date(last_date_raw, field=f"by_source.{source_name}.last_date")
            if last_date < first_date:
                raise ProviderPayloadError("historical source last_date must not precede first_date")
            if first_date < requested_from or last_date > requested_to:
                raise ProviderPayloadError("historical source dates fall outside requested window")
            sources.append(
                HistoricalCoverageSource(
                    source=source_name,
                    rows=rows,
                    first_date=first_date_raw,
                    last_date=last_date_raw,
                    priced_rows=priced_rows,
                )
            )

        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        api_version = _header(response.headers, "x-api-version")
        return HistoricalCoverageReport(
            sport_key=self.sport_key,
            date_from=date_from,
            date_to=date_to,
            historical_window_hours=window_hours,
            historical_window_from=window_from_raw,
            observed_at=observed_at,
            response_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            sources=tuple(sources),
            api_version=api_version,
        )

    def _fetch(self) -> HttpJsonResponse:
        return self._request(self._url())

    def _request(self, url: str) -> HttpJsonResponse:
        headers = {"Accept": "application/json", "User-Agent": "Autosport/0.1 read-only-market-observer"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.transport(url, headers, self.timeout_seconds)
                if type(response.status_code) is not int or not 100 <= response.status_code <= 599:
                    raise ProviderPayloadError("provider transport returned invalid HTTP status")
                if not 200 <= response.status_code < 300:
                    raise ProviderTransportError(
                        f"provider HTTP {response.status_code}",
                        response.status_code,
                        _parse_retry_after(_header(response.headers, "retry-after")),
                    )
                return response
            except ProviderTransportError as exc:
                retryable = exc.status_code == 429 or (exc.status_code is not None and exc.status_code >= 500)
                if not retryable or attempt >= self.max_attempts:
                    raise
                requested = exc.retry_after if exc.retry_after is not None else 0.25 * attempt
                if (
                    isinstance(requested, bool)
                    or not isinstance(requested, (int, float))
                    or not math.isfinite(float(requested))
                ):
                    requested = 0.25 * attempt
                self.sleeper(min(max(0.0, float(requested)), self.max_backoff_seconds))
        raise AssertionError("unreachable")

    def _url(self) -> str:
        query = urlencode(
            {
                "regions": ",".join(self.regions),
                "markets": ",".join(self.markets),
                "oddsFormat": "decimal",
                "include": "slim",
            }
        )
        if self.public_preview:
            return f"{self.base_url}/v1/try/table_tennis/odds?{query}"
        return f"{self.base_url}/v1/sports/table_tennis/odds?{query}"

    @staticmethod
    def _event_list(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            events = payload
        elif isinstance(payload, dict) and isinstance(payload.get("events"), list):
            events = payload["events"]
        else:
            raise ProviderPayloadError("provider response must be an event list or an object containing events[]")
        if not all(isinstance(event, dict) for event in events):
            raise ProviderPayloadError("provider events must be objects")
        return events

    def _event_quotes(
        self,
        event: dict[str, Any],
        observed_ts: str,
        http_status: int,
        provider_state: ProviderStateSnapshot | None,
    ) -> list[ProviderQuote]:
        if "id" in event:
            raw_event_id = event["id"]
        elif "canonical_event_id" in event:
            raw_event_id = event["canonical_event_id"]
        else:
            raise ProviderPayloadError("event is missing id")
        event_id = _provider_identity(raw_event_id, field="event id", allow_colon=False)
        bookmakers = event.get("bookmakers", [])
        if not isinstance(bookmakers, list):
            raise ProviderPayloadError("event bookmakers must be a list")
        output: list[ProviderQuote] = []
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict):
                raise ProviderPayloadError("bookmaker entries must be objects")
            if "key" in bookmaker:
                raw_book_key = bookmaker["key"]
            elif "title" in bookmaker:
                raw_book_key = bookmaker["title"]
            else:
                raise ProviderPayloadError("bookmaker is missing key/title identity")
            book_key = _provider_identity(raw_book_key, field="bookmaker identity")
            source_state = provider_state.for_source(book_key) if provider_state is not None else None
            if provider_state is not None and (source_state is None or source_state.role == "offline"):
                continue
            markets = bookmaker.get("markets", [])
            if not isinstance(markets, list):
                raise ProviderPayloadError("bookmaker markets must be a list")
            for market in markets:
                if not isinstance(market, dict):
                    raise ProviderPayloadError("market entries must be objects")
                raw_market_key = market.get("key")
                if raw_market_key is None or raw_market_key == "":
                    raise ProviderPayloadError("market is missing key")
                market_key = _provider_identity(raw_market_key, field="market key")
                source_ts = _first_nonempty(market.get("last_update"), bookmaker.get("last_update"))
                outcomes = market.get("outcomes", [])
                if not isinstance(outcomes, list):
                    raise ProviderPayloadError("market outcomes must be a list")
                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        raise ProviderPayloadError("outcome entries must be objects")
                    raw_selection = outcome.get("name")
                    if raw_selection is None or raw_selection == "":
                        raise ProviderPayloadError("outcome is missing name")
                    selection = _provider_identity(raw_selection, field="outcome name")
                    raw_price = outcome.get("price")
                    price = _decimal_price(raw_price)
                    if raw_price is not None and price is None:
                        raise ProviderPayloadError(
                            "outcome price must be finite decimal odds greater than 1"
                        )
                    if price is None:
                        continue
                    raw_point = outcome.get("point")
                    point = _decimal_optional(raw_point)
                    if raw_point is not None and point is None:
                        raise ProviderPayloadError("outcome point must be a finite decimal")
                    provider_market_id = _market_identity(book_key, market_key, point)
                    sequence = _sequence_from_timestamp(source_ts or observed_ts)
                    metadata = {
                        "provider": "parlayapi",
                        "sport_key": event.get("sport_key", "table_tennis"),
                        "sport_title": event.get("sport_title"),
                        "commence_time": event.get("commence_time"),
                        "home_team": event.get("home_team"),
                        "away_team": event.get("away_team"),
                        "bookmaker_key": book_key,
                        "bookmaker_title": bookmaker.get("title"),
                        "market_key": market_key,
                        "line": str(point) if point is not None else None,
                        "requested_odds_format": "decimal",
                        "public_preview": self.public_preview,
                        "http_status": http_status,
                    }
                    if source_state is not None and provider_state is not None:
                        metadata.update(
                            {
                                "provider_state_role": source_state.role,
                                "provider_state_age_seconds": source_state.age_seconds,
                                "provider_state_server_timestamp": provider_state.server_timestamp,
                                "provider_state_truncated": provider_state.truncated,
                            }
                        )
                    output.append(
                        ProviderQuote(
                            provider_event_id=event_id,
                            provider_market_id=provider_market_id,
                            provider_selection_id=selection,
                            decimal_odds=price,
                            observed_ts=observed_ts,
                            sequence=sequence,
                            market_type=_market_type(market_key),
                            status="open",
                            source_ts=source_ts,
                            metadata=metadata,
                            sport=self.sport_key,
                        )
                    )
        return output


def _parse_provider_state(headers: Mapping[str, str]) -> ProviderStateSnapshot | None:
    raw = _header(headers, "x-provider-state")
    if raw is None:
        return None
    payload = _decode_provider_json(raw.encode("utf-8"))
    if not isinstance(payload, dict):
        raise ProviderPayloadError("x-provider-state must be a JSON object")
    server_timestamp = payload.get("ts")
    if type(server_timestamp) is not int or server_timestamp <= 0:
        raise ProviderPayloadError("x-provider-state ts must be a positive integer unix timestamp")
    raw_sources = payload.get("src")
    if not isinstance(raw_sources, dict):
        raise ProviderPayloadError("x-provider-state src must be an object")
    truncated = payload.get("truncated", False)
    if type(truncated) is not bool:
        raise ProviderPayloadError("x-provider-state truncated must be boolean")

    sources: list[ProviderSourceState] = []
    for raw_source, raw_state in sorted(raw_sources.items(), key=lambda item: str(item[0])):
        source = _provider_identity(raw_source, field="provider state source")
        if not isinstance(raw_state, dict):
            raise ProviderPayloadError(f"provider state for {source!r} must be an object")
        role = raw_state.get("role")
        if type(role) is not str or role not in {"primary", "degraded", "offline"}:
            raise ProviderPayloadError(f"provider state role for {source!r} is invalid")
        raw_age = raw_state.get("age_s")
        if raw_age is None:
            age_seconds = None
        else:
            if isinstance(raw_age, bool) or not isinstance(raw_age, (int, float)):
                raise ProviderPayloadError(f"provider state age_s for {source!r} must be a number or null")
            age_seconds = float(raw_age)
            if not math.isfinite(age_seconds) or age_seconds < 0:
                raise ProviderPayloadError(f"provider state age_s for {source!r} must be finite and non-negative")
        if age_seconds is None and role != "offline":
            raise ProviderPayloadError(
                f"provider state age_s for {source!r} may be null only when role is offline"
            )
        sources.append(ProviderSourceState(source, age_seconds, role))
    return ProviderStateSnapshot(server_timestamp, tuple(sources), truncated)


def _provider_state_quality_flags(
    events: list[dict[str, Any]],
    provider_state: ProviderStateSnapshot | None,
) -> tuple[str, ...]:
    if provider_state is None:
        return ("PROVIDER_STATE_MISSING",)

    flags: set[str] = set()
    if provider_state.truncated:
        flags.add("PROVIDER_STATE_TRUNCATED")
    if any(state.role == "degraded" for state in provider_state.sources):
        flags.add("UPSTREAM_SOURCE_DEGRADED")
    if any(state.role == "offline" for state in provider_state.sources):
        flags.add("UPSTREAM_SOURCE_OFFLINE")

    # Detect response rows whose source has no same-response state without moving
    # payload-shape validation ahead of the existing lazy quote materialization.
    for event in events:
        bookmakers = event.get("bookmakers")
        if not isinstance(bookmakers, list):
            continue
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict):
                continue
            raw_book_key = bookmaker.get("key", bookmaker.get("title"))
            if not isinstance(raw_book_key, str) or not raw_book_key or raw_book_key != raw_book_key.strip():
                continue
            if "|" in raw_book_key:
                continue
            if provider_state.for_source(raw_book_key) is None:
                flags.add("PROVIDER_STATE_UNCOVERED_SOURCE")
    return tuple(sorted(flags))


def _market_type(key: str) -> MarketType:
    normalized = key.strip().lower()
    if normalized in {"h2h", "moneyline", "player_moneyline", "money_line", "game_winner"}:
        return MarketType.WINNER
    if normalized == "spreads" or "spread" in normalized or "handicap" in normalized:
        return MarketType.HANDICAP
    if normalized == "totals" or "total" in normalized:
        return MarketType.TOTAL
    return MarketType.OTHER


def _market_identity(book_key: str, market_key: str, point: Decimal | None) -> str:
    if point is None or _market_type(market_key) is MarketType.WINNER:
        return f"{book_key}:{market_key}"
    line = abs(point) if _market_type(market_key) is MarketType.HANDICAP else point
    return f"{book_key}:{market_key}:{line}"


def _decimal_price(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 1:
        return None
    return value


def _decimal_optional(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _first_nonempty(*values: Any) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _sequence_from_timestamp(value: str) -> int:
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1_000_000)
    except (ValueError, OverflowError) as exc:
        raise ProviderPayloadError(f"invalid provider timestamp: {value!r}") from exc


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed):
        return None
    return max(0.0, parsed)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target and str(value).strip():
            return str(value).strip()
    return None


def _parse_iso_date(value: str, *, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ProviderPayloadError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ProviderPayloadError(f"{field} must be canonical YYYY-MM-DD")
    return parsed


def _parse_provider_date(value: str, *, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProviderPayloadError(f"{field} must be an ISO date or timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ProviderPayloadError(f"{field} timestamp must include timezone")
        return parsed.date()


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderPayloadError(f"{field} must be a non-negative integer")
    return value