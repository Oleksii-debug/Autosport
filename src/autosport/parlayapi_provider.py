from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .domain import MarketType, utc_now_iso
from .providers import ProviderBatch, ProviderQuote


class ProviderPayloadError(ValueError):
    pass


class ProviderTransportError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class HttpJsonResponse:
    payload: Any
    status_code: int
    headers: Mapping[str, str]


Transport = Callable[[str, Mapping[str, str], float], HttpJsonResponse]
Clock = Callable[[], str]
Sleeper = Callable[[float], None]


def _default_transport(url: str, headers: Mapping[str, str], timeout: float) -> HttpJsonResponse:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed HTTPS base URL by default
            raw = response.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProviderPayloadError("provider returned invalid JSON") from exc
            return HttpJsonResponse(payload, int(response.status), dict(response.headers.items()))
    except HTTPError as exc:
        retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
        raise ProviderTransportError(f"provider HTTP {exc.code}", int(exc.code), retry_after) from exc
    except URLError as exc:
        raise ProviderTransportError(f"provider transport error: {exc.reason}") from exc


class ParlayApiTableTennisProvider:
    """Read-only table-tennis odds adapter. No bookmaker account or wager execution capability exists here."""

    source_id = "parlayapi:table_tennis"

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
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts < 1 or max_attempts > 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if max_backoff_seconds < 0:
            raise ValueError("max_backoff_seconds must be non-negative")
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

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        observed_ts = self.clock()
        response = self._fetch()
        events = self._event_list(response.payload)
        quotes: list[ProviderQuote] = []
        for event in events:
            for quote in self._event_quotes(event, observed_ts, response.status_code):
                if len(quotes) >= max_items:
                    return ProviderBatch(
                        self.source_id,
                        tuple(quotes),
                        cursor=observed_ts,
                        quality_flags=("TRUNCATED_BATCH",),
                    )
                quotes.append(quote)
        return ProviderBatch(self.source_id, tuple(quotes), cursor=observed_ts)

    def _fetch(self) -> HttpJsonResponse:
        url = self._url()
        headers = {"Accept": "application/json", "User-Agent": "Autosport/0.1 read-only-market-observer"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self.transport(url, headers, self.timeout_seconds)
            except ProviderTransportError as exc:
                retryable = exc.status_code == 429 or (exc.status_code is not None and exc.status_code >= 500)
                if not retryable or attempt >= self.max_attempts:
                    raise
                requested = exc.retry_after if exc.retry_after is not None else 0.25 * attempt
                self.sleeper(min(max(0.0, requested), self.max_backoff_seconds))
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

    def _event_quotes(self, event: dict[str, Any], observed_ts: str, http_status: int) -> list[ProviderQuote]:
        event_id = str(event.get("id") or event.get("canonical_event_id") or "").strip()
        if not event_id:
            raise ProviderPayloadError("event is missing id")
        bookmakers = event.get("bookmakers", [])
        if not isinstance(bookmakers, list):
            raise ProviderPayloadError("event bookmakers must be a list")
        output: list[ProviderQuote] = []
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict):
                continue
            book_key = str(bookmaker.get("key") or bookmaker.get("title") or "unknown-book").strip()
            markets = bookmaker.get("markets", [])
            if not isinstance(markets, list):
                continue
            for market in markets:
                if not isinstance(market, dict):
                    continue
                market_key = str(market.get("key") or "").strip()
                if not market_key:
                    continue
                source_ts = _first_nonempty(market.get("last_update"), bookmaker.get("last_update"))
                outcomes = market.get("outcomes", [])
                if not isinstance(outcomes, list):
                    continue
                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue
                    selection = str(outcome.get("name") or "").strip()
                    if not selection:
                        continue
                    price = _decimal_price(outcome.get("price"))
                    if price is None:
                        continue
                    point = _decimal_optional(outcome.get("point"))
                    provider_market_id = _market_identity(book_key, market_key, point)
                    sequence = _sequence_from_timestamp(source_ts or observed_ts)
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
                            metadata={
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
                            },
                        )
                    )
        return output


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
        return max(0.0, float(value))
    except ValueError:
        return None
