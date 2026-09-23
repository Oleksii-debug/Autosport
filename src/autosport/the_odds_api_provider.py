"""Read-only The Odds API v4 adapter with causal multi-sport provenance.

The adapter only observes provider market data. It exposes no account, wager,
settlement, bankroll, or provider-write operation and cannot confer real-money authority.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .domain import MarketType, utc_now_iso
from .providers import ProviderBatch, ProviderQuote, ProviderUnavailableError


THE_ODDS_API_BASE_URL = "https://api.the-odds-api.com"
THE_ODDS_API_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
THE_ODDS_API_MAX_DECIMAL_TEXT_CHARS = 4096
THE_ODDS_API_TERMS_SOURCE_REF = "https://the-odds-api.com/terms-and-conditions.html"
THE_ODDS_API_TERMS_LAST_UPDATED = "2026-08-31"
THE_ODDS_API_DOCS_SOURCE_REF = "https://the-odds-api.com/liveapi/guides/v4/"


class TheOddsApiPayloadError(ValueError):
    """Provider payload violates the bounded read-only adapter contract."""


class TheOddsApiTransportError(ProviderUnavailableError):
    """Typed transport/auth/quota/provider-unavailable failure."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        *,
        provider_error_code: str | None = None,
        quota_remaining: int | None = None,
        quota_used: int | None = None,
        quota_last: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_error_code = provider_error_code
        self.quota_remaining = quota_remaining
        self.quota_used = quota_used
        self.quota_last = quota_last


@dataclass(frozen=True, slots=True)
class HttpJsonResponse:
    payload: Any
    status_code: int
    headers: Mapping[str, str]
    final_url: str | None = None
    body_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class TheOddsApiRequestEvidence:
    endpoint_kind: str
    sport: str
    regions: tuple[str, ...]
    bookmakers: tuple[str, ...]
    markets: tuple[str, ...]
    event_ids: tuple[str, ...]
    include_sids: bool
    include_bet_limits: bool
    observed_at: str
    provider_origin_verified: bool
    receipt_clock_verified: bool
    response_sha256: str | None
    requested_snapshot_at: str | None = None
    actual_snapshot_at: str | None = None
    quota_remaining: int | None = None
    quota_used: int | None = None
    quota_last: int | None = None

    @property
    def effective_bookmaker_scope(self) -> str:
        return "bookmakers" if self.bookmakers else "regions"

    @property
    def request_fingerprint(self) -> str:
        payload = {
            "actual_snapshot_at": self.actual_snapshot_at,
            "bookmakers": list(self.bookmakers),
            "date_format": "iso",
            "endpoint_kind": self.endpoint_kind,
            "event_ids": list(self.event_ids),
            "include_bet_limits": self.include_bet_limits,
            "include_sids": self.include_sids,
            "markets": list(self.markets),
            "odds_format": "decimal",
            "provider_origin_verified": self.provider_origin_verified,
            "receipt_clock_verified": self.receipt_clock_verified,
            "regions": list(self.regions),
            "requested_snapshot_at": self.requested_snapshot_at,
            "scope_precedence": self.effective_bookmaker_scope,
            "sport": self.sport,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def metadata(self) -> dict[str, Any]:
        return {
            "endpoint_kind": self.endpoint_kind,
            "sport": self.sport,
            "regions": list(self.regions),
            "bookmakers": list(self.bookmakers),
            "effective_bookmaker_scope": self.effective_bookmaker_scope,
            "markets": list(self.markets),
            "event_ids": list(self.event_ids),
            "odds_format": "decimal",
            "date_format": "iso",
            "include_sids": self.include_sids,
            "include_bet_limits": self.include_bet_limits,
            "observed_at": self.observed_at,
            "provider_origin_verified": self.provider_origin_verified,
            "receipt_clock_verified": self.receipt_clock_verified,
            "response_sha256": self.response_sha256,
            "requested_snapshot_at": self.requested_snapshot_at,
            "actual_snapshot_at": self.actual_snapshot_at,
            "request_fingerprint": self.request_fingerprint,
            "quota": {
                "remaining": self.quota_remaining,
                "used": self.quota_used,
                "last": self.quota_last,
            },
        }


    def source_metadata(self) -> dict[str, Any]:
        """Request/source evidence safe to bind into durable per-quote payload.

        Receipt time, quota counters and the whole-response digest describe this local
        acquisition, not the provider quote at its source sequence. They remain
        available on TheOddsApiRequestEvidence and the batch cursor instead of making
        an unchanged provider quote conflict with itself on the next poll.
        """

        metadata = self.metadata()
        metadata.pop("observed_at", None)
        metadata.pop("response_sha256", None)
        metadata.pop("quota", None)
        return metadata


@dataclass(frozen=True, slots=True)
class TheOddsApiHistoricalSnapshot:
    requested_at: str
    snapshot_at: str
    previous_snapshot_at: str | None
    next_snapshot_at: str | None
    batch: ProviderBatch
    request_evidence: TheOddsApiRequestEvidence


Transport = Callable[[str, float], HttpJsonResponse]
Clock = Callable[[], str]


def _decode_provider_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TheOddsApiPayloadError("provider returned invalid UTF-8 JSON") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TheOddsApiPayloadError(
                    f"provider returned duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    def reject_non_finite(value: str) -> None:
        raise TheOddsApiPayloadError(
            f"provider returned non-standard JSON constant {value!r}"
        )

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=reject_non_finite,
        )
    except (json.JSONDecodeError, InvalidOperation) as exc:
        raise TheOddsApiPayloadError("provider returned invalid JSON") from exc


def _failure_error_code(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("error_code")
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 128
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
            for character in value
        )
    ):
        return None
    return value


def _failure_quota_header(
    headers: Mapping[str, str],
    name: str,
) -> int | None:
    wanted = name.lower()
    raw: str | None = None
    for key, value in headers.items():
        if str(key).lower() == wanted:
            raw = str(value)
            break
    if (
        raw is None
        or not raw
        or len(raw) > 32
        or not raw.isascii()
        or not raw.isdigit()
    ):
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed


def _failure_evidence(
    payload: object,
    headers: Mapping[str, str],
) -> tuple[str | None, int | None, int | None, int | None]:
    return (
        _failure_error_code(payload),
        _failure_quota_header(headers, "x-requests-remaining"),
        _failure_quota_header(headers, "x-requests-used"),
        _failure_quota_header(headers, "x-requests-last"),
    )


class _RejectRedirects(HTTPRedirectHandler):
    """Reject redirects before a credential-bearing second HTTP hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


def _canonical_provider_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "api.the-odds-api.com"
        or not parts.path.startswith("/v4/")
        or parts.fragment
    ):
        raise TheOddsApiTransportError(
            "The Odds API transport received a non-canonical provider URL"
        )


def _bounded_response_body(response: object) -> bytes:
    raw_body = response.read(THE_ODDS_API_MAX_RESPONSE_BYTES + 1)
    if type(raw_body) is not bytes:
        raise TheOddsApiPayloadError("provider response body must be exact bytes")
    if len(raw_body) > THE_ODDS_API_MAX_RESPONSE_BYTES:
        raise TheOddsApiPayloadError("The Odds API response exceeds bounded size")
    return raw_body


def _make_product_redirect_handler_factory() -> type[HTTPRedirectHandler]:
    """Create a redirect handler class with the original refusal method captured."""

    class ProductRejectRedirects(HTTPRedirectHandler):
        redirect_request = _RejectRedirects.redirect_request

    return ProductRejectRedirects


def _perform_http_json_response(
    url: str,
    timeout: float,
    *,
    opener_factory: Callable[..., Any],
    proxy_handler_factory: Callable[..., Any],
    request_factory: Callable[..., Any],
    redirect_handler_factory: Callable[..., Any] = _make_product_redirect_handler_factory(),
    canonical_url_validator: Callable[[str], None] = _canonical_provider_url,
    bounded_body_reader: Callable[[object], bytes] = _bounded_response_body,
    json_decoder: Callable[[bytes], Any] = _decode_provider_json,
    digest_factory: Callable[[bytes], Any] = hashlib.sha256,
    failure_evidence_builder: Callable[
        [object, Mapping[str, str]],
        tuple[str | None, int | None, int | None, int | None],
    ] = _failure_evidence,
) -> HttpJsonResponse:
    """Perform one bounded HTTPS read through explicit, captured dependencies.

    This helper itself is not provider-origin authority. The product default transport
    closes over the real urllib constructors, while the remaining authority-critical
    validators/parsers are captured here as function defaults instead of late-bound
    module globals. Tests can inject a fake network stack only by using a different
    transport function, which is classified as unverified by TheOddsApiProvider.
    """

    canonical_url_validator(url)
    request = request_factory(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Autosport/0.1 read-only-market-observer",
        },
        method="GET",
    )
    http_status: int | None = None
    provider_error_code: str | None = None
    quota_remaining: int | None = None
    quota_used: int | None = None
    quota_last: int | None = None
    transport_failed = False
    opener = opener_factory(
        proxy_handler_factory({}),
        redirect_handler_factory(),
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = str(response.geturl())
            if final_url != url:
                raise TheOddsApiTransportError(
                    "The Odds API final URL does not match the canonical request URL"
                )
            raw_body = bounded_body_reader(response)
            return HttpJsonResponse(
                payload=json_decoder(raw_body),
                status_code=int(response.status),
                headers=dict(response.headers.items()),
                final_url=final_url,
                body_sha256=digest_factory(raw_body).hexdigest(),
            )
    except HTTPError as exc:
        http_status = int(exc.code)
        failure_headers = (
            {}
            if exc.headers is None
            else dict(exc.headers.items())
        )
        failure_payload: object = None
        if getattr(exc, "fp", None) is not None:
            try:
                failure_body = bounded_body_reader(exc)
                failure_payload = json_decoder(failure_body)
            except (OSError, TypeError, ValueError):
                # The request already failed. Malformed/oversized failure bodies
                # cannot become success or leak the credential-bearing URL.
                failure_payload = None
        (
            provider_error_code,
            quota_remaining,
            quota_used,
            quota_last,
        ) = failure_evidence_builder(failure_payload, failure_headers)
    except (URLError, TimeoutError, OSError):
        transport_failed = True

    if http_status is not None:
        raise TheOddsApiTransportError(
            f"The Odds API HTTP {http_status}",
            http_status,
            provider_error_code=provider_error_code,
            quota_remaining=quota_remaining,
            quota_used=quota_used,
            quota_last=quota_last,
        )
    if transport_failed:
        raise TheOddsApiTransportError("The Odds API transport unavailable")
    raise AssertionError("unreachable transport outcome")


def _build_product_transport(
    opener_factory: Callable[..., Any],
    proxy_handler_factory: Callable[..., Any],
    request_factory: Callable[..., Any],
    performer: Callable[..., HttpJsonResponse],
) -> Transport:
    """Capture the product-owned network stack outside caller-mutable module globals."""

    def product_transport(url: str, timeout: float) -> HttpJsonResponse:
        return performer(
            url,
            timeout,
            opener_factory=opener_factory,
            proxy_handler_factory=proxy_handler_factory,
            request_factory=request_factory,
        )

    return product_transport


_default_transport = _build_product_transport(
    build_opener,
    ProxyHandler,
    Request,
    _perform_http_json_response,
)
def _plain_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise TheOddsApiPayloadError(f"{field} must be a non-empty trimmed string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TheOddsApiPayloadError(f"{field} must be UTF-8 encodable") from exc
    return value


def _canonical_component(
    value: object,
    field: str,
    *,
    allow_colon: bool = True,
) -> str:
    text = _plain_text(value, field)
    if "|" in text:
        raise TheOddsApiPayloadError(
            f"{field} must not contain reserved identity delimiter '|'"
        )
    if not allow_colon and ":" in text:
        raise TheOddsApiPayloadError(
            f"{field} must not contain reserved source-scope delimiter ':'"
        )
    return text


def _optional_provider_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _plain_text(value, field)


def _sport(value: object, field: str = "sport") -> str:
    text = _canonical_component(value, field)
    if text != text.lower() or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in text
    ):
        raise TheOddsApiPayloadError(
            f"{field} must be a lowercase canonical sport key"
        )
    if text in {"unknown", "mixed"}:
        raise TheOddsApiPayloadError(
            f"{field} must not use a reserved sport identity"
        )
    return text


def _timestamp(value: object, field: str) -> str:
    text = _plain_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TheOddsApiPayloadError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TheOddsApiPayloadError(
            f"{field} must be timezone-aware ISO-8601"
        )
    return text


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _chronological_microseconds(value: str, field: str) -> int:
    """Map one provider timestamp to exact UTC microseconds for source ordering."""

    try:
        instant = _datetime(_timestamp(value, field)).astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise TheOddsApiPayloadError(
            f"{field} cannot be represented as a UTC source sequence"
        ) from exc
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = instant - epoch
    sequence = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    if sequence < -(1 << 63) or sequence > (1 << 63) - 1:
        raise TheOddsApiPayloadError(f"{field} source sequence exceeds signed 64-bit range")
    return sequence


def _decimal(
    value: object,
    field: str,
    *,
    greater_than_one: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TheOddsApiPayloadError(
            f"{field} must enter through exact Decimal/string JSON parsing"
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str) and value and value == value.strip():
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise TheOddsApiPayloadError(
                f"{field} must be a finite decimal"
            ) from exc
    else:
        raise TheOddsApiPayloadError(f"{field} must be a finite decimal")
    if not result.is_finite():
        raise TheOddsApiPayloadError(f"{field} must be finite")
    if greater_than_one and result <= 1:
        raise TheOddsApiPayloadError(f"{field} must be greater than 1")
    if nonnegative and result < 0:
        raise TheOddsApiPayloadError(f"{field} must be non-negative")
    return result


def _decimal_text(value: Decimal) -> str:
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise TheOddsApiPayloadError("provider decimal must be finite")
    digit_count = len(digits)
    sign_chars = 1 if sign else 0
    if exponent >= 0:
        text_length = sign_chars + digit_count + exponent
    elif digit_count + exponent > 0:
        text_length = sign_chars + digit_count + 1
    else:
        text_length = sign_chars + 2 - exponent
    if text_length > THE_ODDS_API_MAX_DECIMAL_TEXT_CHARS:
        raise TheOddsApiPayloadError(
            "provider decimal fixed-point representation exceeds bounded size"
        )
    return format(value, "f")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _optional_sha256(value: object, field: str) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TheOddsApiPayloadError(
            f"{field} must be a lowercase hexadecimal SHA-256 digest"
        )
    return value


def _quota_header(headers: Mapping[str, str], name: str) -> int | None:
    raw = _header(headers, name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise TheOddsApiPayloadError(
            f"{name} must be a non-negative integer"
        ) from exc
    if value < 0:
        raise TheOddsApiPayloadError(
            f"{name} must be a non-negative integer"
        )
    return value


def _identity_digest(*values: object) -> str:
    encoded = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _market_type(market_key: str) -> MarketType:
    if market_key in {"h2h", "h2h_lay", "outrights", "outrights_lay"}:
        return MarketType.WINNER
    if market_key == "spreads":
        return MarketType.HANDICAP
    if market_key == "totals":
        return MarketType.TOTAL
    return MarketType.OTHER


_IMPLICIT_RESPONSE_MARKET_PAIRS = (
    ("h2h", "h2h_lay"),
    ("outrights", "outrights_lay"),
)


def _response_market_scope(requested_markets: tuple[str, ...]) -> frozenset[str]:
    scope = set(requested_markets)
    for requested, implicit in _IMPLICIT_RESPONSE_MARKET_PAIRS:
        if requested in scope:
            scope.add(implicit)
    return frozenset(scope)


class TheOddsApiProvider:
    """Observation-only adapter for The Odds API v4 current and historical odds."""

    def __init__(
        self,
        api_key: str,
        *,
        sport: str,
        regions: Sequence[str] = ("eu",),
        bookmakers: Sequence[str] = (),
        markets: Sequence[str] = ("h2h",),
        event_ids: Sequence[str] = (),
        include_sids: bool = True,
        include_bet_limits: bool = False,
        max_market_age_seconds: int | None = None,
        base_url: str = THE_ODDS_API_BASE_URL,
        timeout_seconds: float = 10.0,
        transport: Transport = _default_transport,
        clock: Clock = utc_now_iso,
    ) -> None:
        if type(api_key) is not str or not api_key or api_key != api_key.strip():
            raise ValueError("api_key must be a non-empty trimmed string")
        requested_sport = _sport(sport, "sport")
        self._regions = self._config_values(regions, "regions", allow_empty=True)
        self._bookmakers = self._config_values(
            bookmakers, "bookmakers", allow_empty=True
        )
        self._markets = self._config_values(markets, "markets")
        self._event_ids = self._config_values(
            event_ids,
            "event_ids",
            allow_empty=True,
            allow_colon=False,
        )
        if not self.bookmakers and not self.regions:
            raise ValueError(
                "regions must be non-empty when bookmakers are not configured"
            )
        if type(include_sids) is not bool or type(include_bet_limits) is not bool:
            raise TypeError("include_sids and include_bet_limits must be bool")
        if max_market_age_seconds is not None and (
            type(max_market_age_seconds) is not int
            or max_market_age_seconds <= 0
        ):
            raise ValueError(
                "max_market_age_seconds must be a positive non-boolean integer or None"
            )
        if (
            isinstance(timeout_seconds, bool)
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite positive number")
        if type(base_url) is not str or base_url.rstrip("/") != THE_ODDS_API_BASE_URL:
            raise ValueError("base_url must be the canonical The Odds API HTTPS origin")
        self.api_key = api_key
        self._sport = requested_sport
        self._include_sids = include_sids
        self._include_bet_limits = include_bet_limits
        self.max_market_age_seconds = max_market_age_seconds
        self.base_url = THE_ODDS_API_BASE_URL
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport
        self.clock = clock
        self.source_id = self._stream_source_id("current")
        self._pending_quotes: tuple[ProviderQuote, ...] = ()
        self._pending_offset = 0
        self._pending_cursor: str | None = None
        self._pending_quality_flags: tuple[str, ...] = ()
        self._last_request_evidence: TheOddsApiRequestEvidence | None = None

    @staticmethod
    def _config_values(
        values: Sequence[str],
        field: str,
        *,
        allow_empty: bool = False,
        allow_colon: bool = True,
    ) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)):
            raise TypeError(f"{field} must be a sequence of strings, not a scalar")
        result = tuple(
            _canonical_component(value, field, allow_colon=allow_colon)
            for value in values
        )
        if not result and not allow_empty:
            raise ValueError(f"{field} must not be empty")
        if len(set(result)) != len(result):
            raise ValueError(f"{field} must not contain duplicates")
        return result

    @property
    def sport(self) -> str:
        return self._sport

    @property
    def regions(self) -> tuple[str, ...]:
        return self._regions

    @property
    def bookmakers(self) -> tuple[str, ...]:
        return self._bookmakers

    @property
    def markets(self) -> tuple[str, ...]:
        return self._markets

    @property
    def event_ids(self) -> tuple[str, ...]:
        return self._event_ids

    @property
    def include_sids(self) -> bool:
        return self._include_sids

    @property
    def include_bet_limits(self) -> bool:
        return self._include_bet_limits

    @property
    def last_request_evidence(self) -> TheOddsApiRequestEvidence | None:
        return self._last_request_evidence

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be a positive non-boolean integer")
        if self._pending_offset >= len(self._pending_quotes):
            response, provider_origin_verified = self._request(
                self._current_url()
            )
            observed_at, receipt_clock_verified = self._capture_receipt_time(
                self.clock
            )
            evidence = self._request_evidence(
                "current",
                observed_at,
                response.headers,
                response_sha256=response.body_sha256,
                provider_origin_verified=provider_origin_verified,
                receipt_clock_verified=receipt_clock_verified,
            )
            self._pending_quotes = self._parse_current_payload(
                response.payload, observed_at, evidence
            )
            self._pending_offset = 0
            self._pending_cursor = (
                evidence.response_sha256 or evidence.request_fingerprint
            )
            self._pending_quality_flags = self._provenance_quality_flags(evidence)
            self._last_request_evidence = evidence

        start = self._pending_offset
        end = min(start + max_items, len(self._pending_quotes))
        quotes = self._pending_quotes[start:end]
        self._pending_offset = end
        flags = ["DYNAMIC_COVERAGE", *self._pending_quality_flags]
        if not quotes and not self._pending_quotes:
            flags.append("EMPTY_RESPONSE")
        if self._pending_offset < len(self._pending_quotes):
            flags.append("TRUNCATED_BATCH")
        cursor = self._pending_cursor
        if self._pending_offset >= len(self._pending_quotes):
            self._clear_pending_snapshot()
        return ProviderBatch(
            self.source_id,
            quotes,
            cursor=cursor,
            quality_flags=tuple(flags),
        )

    def read_historical_snapshot(
        self,
        requested_at: str,
        *,
        max_items: int = 100000,
    ) -> TheOddsApiHistoricalSnapshot:
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be a positive non-boolean integer")
        requested_at = _timestamp(requested_at, "requested_at")
        response, provider_origin_verified = self._request(
            self._historical_url(requested_at)
        )
        observed_at, receipt_clock_verified = self._capture_receipt_time(
            self.clock
        )
        if _datetime(requested_at) > _datetime(observed_at):
            raise TheOddsApiPayloadError(
                "historical requested_at cannot be later than local receipt time"
            )
        payload = response.payload
        if not isinstance(payload, dict):
            raise TheOddsApiPayloadError("historical response must be an object")

        snapshot_at = _timestamp(payload.get("timestamp"), "timestamp")
        if _datetime(snapshot_at) > _datetime(requested_at):
            raise TheOddsApiPayloadError(
                "historical snapshot timestamp must not be later than requested_at"
            )
        previous = payload.get("previous_timestamp")
        next_snapshot = payload.get("next_timestamp")
        previous_at = (
            None if previous is None else _timestamp(previous, "previous_timestamp")
        )
        next_at = (
            None if next_snapshot is None else _timestamp(next_snapshot, "next_timestamp")
        )
        if previous_at is not None and _datetime(previous_at) >= _datetime(snapshot_at):
            raise TheOddsApiPayloadError(
                "previous_timestamp must precede historical snapshot timestamp"
            )
        if next_at is not None and _datetime(next_at) <= _datetime(requested_at):
            raise TheOddsApiPayloadError(
                "next_timestamp must be later than requested_at for closest snapshot evidence"
            )

        events = payload.get("data")
        if not isinstance(events, list):
            raise TheOddsApiPayloadError("historical response data must be an event list")
        evidence = self._request_evidence(
            "historical",
            observed_at,
            response.headers,
            response_sha256=response.body_sha256,
            provider_origin_verified=provider_origin_verified,
            receipt_clock_verified=receipt_clock_verified,
            requested_snapshot_at=requested_at,
            actual_snapshot_at=snapshot_at,
        )
        quotes = self._parse_events(
            events,
            observed_at,
            evidence,
            historical_snapshot_at=snapshot_at,
        )
        if len(quotes) > max_items:
            raise TheOddsApiPayloadError(
                "historical snapshot exceeds max_items; refusing partial evidence"
            )
        flags = [
            "DYNAMIC_COVERAGE",
            "HISTORICAL_SNAPSHOT",
            *self._provenance_quality_flags(evidence),
        ]
        if not quotes:
            flags.append("EMPTY_RESPONSE")
        batch = ProviderBatch(
            self._stream_source_id(
                "historical",
                requested_snapshot_at=requested_at,
            ),
            quotes,
            cursor=evidence.response_sha256 or snapshot_at,
            quality_flags=tuple(flags),
        )
        self._last_request_evidence = evidence
        return TheOddsApiHistoricalSnapshot(
            requested_at=requested_at,
            snapshot_at=snapshot_at,
            previous_snapshot_at=previous_at,
            next_snapshot_at=next_at,
            batch=batch,
            request_evidence=evidence,
        )

    def _clear_pending_snapshot(self) -> None:
        self._pending_quotes = ()
        self._pending_offset = 0
        self._pending_cursor = None
        self._pending_quality_flags = ()

    @staticmethod
    def _capture_receipt_time(
        clock: Clock,
        product_clock: Clock = utc_now_iso,
        product_clock_code: object = utc_now_iso.__code__,
        product_clock_datetime: object = utc_now_iso.__globals__["datetime"],
        product_clock_timezone: object = utc_now_iso.__globals__["timezone"],
    ) -> tuple[str, bool]:
        """Capture receipt time without laundering a mutated product clock."""

        def product_clock_is_intact() -> bool:
            product_globals = getattr(product_clock, "__globals__", None)
            return (
                clock is product_clock
                and getattr(product_clock, "__code__", None) is product_clock_code
                and isinstance(product_globals, dict)
                and product_globals.get("datetime") is product_clock_datetime
                and product_globals.get("timezone") is product_clock_timezone
            )

        product_owned = clock is product_clock
        if product_owned and not product_clock_is_intact():
            raise TheOddsApiTransportError(
                "The Odds API receipt clock authority changed before capture"
            )
        observed_at = _timestamp(clock(), "observed_at")
        if product_owned and not product_clock_is_intact():
            raise TheOddsApiTransportError(
                "The Odds API receipt clock authority changed during capture"
            )
        return observed_at, product_owned

    def _request(
        self,
        url: str,
        product_transport: Transport = _default_transport,
        product_transport_code: object = _default_transport.__code__,
        product_transport_freevars: tuple[str, ...] = _default_transport.__code__.co_freevars,
        product_transport_closure: tuple[object, ...] = tuple(
            cell.cell_contents for cell in (_default_transport.__closure__ or ())
        ),
        product_transport_performer: Callable[..., HttpJsonResponse] = _perform_http_json_response,
        product_transport_performer_code: object = _perform_http_json_response.__code__,
        product_transport_performer_kwdefaults: tuple[tuple[str, object], ...] = tuple(
            sorted((_perform_http_json_response.__kwdefaults__ or {}).items())
        ),
    ) -> tuple[HttpJsonResponse, bool]:
        transport = self.transport

        def product_transport_is_intact() -> bool:
            if transport is not product_transport:
                return False
            if (
                getattr(transport, "__code__", None) is not product_transport_code
                or product_transport_code.co_freevars != product_transport_freevars
            ):
                return False
            closure = getattr(transport, "__closure__", None) or ()
            if len(closure) != len(product_transport_closure):
                return False
            if any(
                cell.cell_contents is not expected
                for cell, expected in zip(closure, product_transport_closure, strict=True)
            ):
                return False
            if (
                getattr(product_transport_performer, "__code__", None)
                is not product_transport_performer_code
            ):
                return False
            current_kwdefaults = product_transport_performer.__kwdefaults__ or {}
            if len(current_kwdefaults) != len(product_transport_performer_kwdefaults):
                return False
            return all(
                key in current_kwdefaults and current_kwdefaults[key] is expected
                for key, expected in product_transport_performer_kwdefaults
            )

        provider_origin_verified = transport is product_transport
        if provider_origin_verified and not product_transport_is_intact():
            raise TheOddsApiTransportError(
                "The Odds API product transport authority changed before request"
            )
        response = transport(url, self.timeout_seconds)
        if provider_origin_verified and not product_transport_is_intact():
            raise TheOddsApiTransportError(
                "The Odds API product transport authority changed during request"
            )
        if type(response) is not HttpJsonResponse:
            raise TypeError("transport must return HttpJsonResponse")
        if response.status_code != 200:
            (
                provider_error_code,
                quota_remaining,
                quota_used,
                quota_last,
            ) = _failure_evidence(response.payload, response.headers)
            raise TheOddsApiTransportError(
                f"The Odds API HTTP {response.status_code}",
                response.status_code,
                provider_error_code=provider_error_code,
                quota_remaining=quota_remaining,
                quota_used=quota_used,
                quota_last=quota_last,
            )
        if provider_origin_verified and response.final_url != url:
            raise TheOddsApiTransportError(
                "The Odds API product transport did not bind the exact final URL"
            )
        body_sha256 = _optional_sha256(
            response.body_sha256,
            "response.body_sha256",
        )
        if provider_origin_verified and body_sha256 is None:
            raise TheOddsApiPayloadError(
                "provider-origin-verified response requires exact response SHA-256"
            )
        return response, provider_origin_verified

    @staticmethod
    def _provenance_quality_flags(
        evidence: TheOddsApiRequestEvidence,
    ) -> tuple[str, ...]:
        flags: list[str] = []
        if not evidence.provider_origin_verified:
            flags.append("UNVERIFIED_PROVIDER_ORIGIN")
        if not evidence.receipt_clock_verified:
            flags.append("UNVERIFIED_RECEIPT_CLOCK")
        return tuple(flags)

    def _stream_source_id(
        self,
        endpoint_kind: str,
        *,
        requested_snapshot_at: str | None = None,
    ) -> str:
        """Return one stable causal stream identity for exact request semantics."""

        if endpoint_kind not in {"current", "historical"}:
            raise ValueError("endpoint_kind must be current or historical")
        if endpoint_kind == "current" and requested_snapshot_at is not None:
            raise ValueError("current stream identity cannot carry a historical cutoff")
        if endpoint_kind == "historical" and requested_snapshot_at is None:
            raise ValueError("historical stream identity requires requested_snapshot_at")

        scope_kind = "bookmakers" if self.bookmakers else "regions"
        scope_values = self.bookmakers if self.bookmakers else self.regions
        normalized_scope = tuple(sorted(scope_values))
        normalized_markets = tuple(sorted(self.markets))
        normalized_event_ids = tuple(sorted(self.event_ids))

        # Preserve the deployed source ID for the exact default current request
        # semantics while reserving every non-default scope a digest-bound ID.
        # This keeps existing default stream continuity without allowing another
        # request scope to alias it.
        if (
            endpoint_kind == "current"
            and scope_kind == "regions"
            and normalized_scope == ("eu",)
            and normalized_markets == ("h2h",)
            and not normalized_event_ids
            and self.include_sids
            and not self.include_bet_limits
        ):
            return f"the-odds-api:{self.sport}"

        normalized_cutoff = None
        if requested_snapshot_at is not None:
            normalized_cutoff = (
                _datetime(requested_snapshot_at)
                .astimezone(timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )

        scope_sha256 = _identity_digest(
            "the-odds-api-source-scope-v1",
            endpoint_kind,
            self.sport,
            scope_kind,
            normalized_scope,
            normalized_markets,
            normalized_event_ids,
            self.include_sids,
            self.include_bet_limits,
            normalized_cutoff,
        )
        return f"the-odds-api:{self.sport}:{endpoint_kind}:{scope_sha256}"

    def _current_url(self) -> str:
        return f"{self.base_url}/v4/sports/{self.sport}/odds?{urlencode(self._query_items())}"

    def _historical_url(self, requested_at: str) -> str:
        items = self._query_items()
        items.append(("date", requested_at))
        return (
            f"{self.base_url}/v4/historical/sports/{self.sport}/odds?"
            f"{urlencode(items)}"
        )

    def _query_items(self) -> list[tuple[str, str]]:
        items = [
            ("apiKey", self.api_key),
            ("markets", ",".join(self.markets)),
            ("oddsFormat", "decimal"),
            ("dateFormat", "iso"),
            ("includeSids", "true" if self.include_sids else "false"),
            ("includeBetLimits", "true" if self.include_bet_limits else "false"),
        ]
        if self.bookmakers:
            items.append(("bookmakers", ",".join(self.bookmakers)))
        else:
            items.append(("regions", ",".join(self.regions)))
        if self.event_ids:
            items.append(("eventIds", ",".join(self.event_ids)))
        return items

    def _request_evidence(
        self,
        endpoint_kind: str,
        observed_at: str,
        headers: Mapping[str, str],
        *,
        response_sha256: str | None,
        provider_origin_verified: bool,
        receipt_clock_verified: bool,
        requested_snapshot_at: str | None = None,
        actual_snapshot_at: str | None = None,
    ) -> TheOddsApiRequestEvidence:
        return TheOddsApiRequestEvidence(
            endpoint_kind=endpoint_kind,
            sport=self.sport,
            regions=self.regions,
            bookmakers=self.bookmakers,
            markets=self.markets,
            event_ids=self.event_ids,
            include_sids=self.include_sids,
            include_bet_limits=self.include_bet_limits,
            observed_at=observed_at,
            provider_origin_verified=provider_origin_verified,
            receipt_clock_verified=receipt_clock_verified,
            response_sha256=_optional_sha256(
                response_sha256,
                "response.body_sha256",
            ),
            requested_snapshot_at=requested_snapshot_at,
            actual_snapshot_at=actual_snapshot_at,
            quota_remaining=_quota_header(headers, "x-requests-remaining"),
            quota_used=_quota_header(headers, "x-requests-used"),
            quota_last=_quota_header(headers, "x-requests-last"),
        )

    def _parse_current_payload(
        self,
        payload: Any,
        observed_at: str,
        evidence: TheOddsApiRequestEvidence,
    ) -> tuple[ProviderQuote, ...]:
        if not isinstance(payload, list):
            raise TheOddsApiPayloadError("current odds response must be an event list")
        return self._parse_events(payload, observed_at, evidence)

    def _parse_events(
        self,
        events: list[Any],
        observed_at: str,
        evidence: TheOddsApiRequestEvidence,
        *,
        historical_snapshot_at: str | None = None,
    ) -> tuple[ProviderQuote, ...]:
        quotes: list[ProviderQuote] = []
        seen: set[tuple[object, ...]] = set()
        response_market_scope = _response_market_scope(evidence.markets)
        for event_index, raw_event in enumerate(events):
            if not isinstance(raw_event, dict):
                raise TheOddsApiPayloadError(
                    f"events[{event_index}] must be an object"
                )
            quotes.extend(
                self._event_quotes(
                    raw_event,
                    observed_at,
                    evidence,
                    seen,
                    response_market_scope,
                    historical_snapshot_at=historical_snapshot_at,
                )
            )
        return tuple(quotes)

    def _event_quotes(
        self,
        event: dict[str, Any],
        observed_at: str,
        evidence: TheOddsApiRequestEvidence,
        seen: set[tuple[object, ...]],
        response_market_scope: frozenset[str],
        *,
        historical_snapshot_at: str | None,
    ) -> Iterator[ProviderQuote]:
        event_id = _canonical_component(
            event.get("id"), "event.id", allow_colon=False
        )
        if evidence.event_ids and event_id not in evidence.event_ids:
            raise TheOddsApiPayloadError(
                "response contains event outside explicit eventIds request scope"
            )
        event_sport = _sport(event.get("sport_key"), "event.sport_key")
        if self.sport != "upcoming" and event_sport != self.sport:
            raise TheOddsApiPayloadError(
                "event sport_key does not match requested sport"
            )
        commence_time = _timestamp(
            event.get("commence_time"), "event.commence_time"
        )
        bookmakers = event.get("bookmakers")
        if not isinstance(bookmakers, list):
            raise TheOddsApiPayloadError("event.bookmakers must be a list")

        causal_reference_at = historical_snapshot_at or observed_at
        causal_reference_field = (
            "historical snapshot" if historical_snapshot_at is not None else "local receipt"
        )

        for bookmaker_index, raw_bookmaker in enumerate(bookmakers):
            if not isinstance(raw_bookmaker, dict):
                raise TheOddsApiPayloadError(
                    f"event.bookmakers[{bookmaker_index}] must be an object"
                )
            bookmaker_key = _canonical_component(
                raw_bookmaker.get("key"), "bookmaker.key"
            )
            if evidence.bookmakers and bookmaker_key not in evidence.bookmakers:
                raise TheOddsApiPayloadError(
                    "response contains bookmaker outside explicit bookmakers request scope"
                )
            bookmaker_event_sid = _optional_provider_text(
                raw_bookmaker.get("sid"), "bookmaker.sid"
            )
            markets = raw_bookmaker.get("markets")
            if not isinstance(markets, list):
                raise TheOddsApiPayloadError("bookmaker.markets must be a list")

            for market_index, raw_market in enumerate(markets):
                if not isinstance(raw_market, dict):
                    raise TheOddsApiPayloadError(
                        f"bookmaker.markets[{market_index}] must be an object"
                    )
                market_key = _canonical_component(
                    raw_market.get("key"), "market.key"
                )
                if market_key not in response_market_scope:
                    raise TheOddsApiPayloadError(
                        "response contains market outside requested markets scope"
                    )
                market_sid = _optional_provider_text(
                    raw_market.get("sid"), "market.sid"
                )
                source_ts = _timestamp(
                    raw_market.get("last_update"), "market.last_update"
                )
                self._validate_freshness(
                    source_ts,
                    causal_reference_at,
                    reference_field=causal_reference_field,
                )
                outcomes = raw_market.get("outcomes")
                if not isinstance(outcomes, list):
                    raise TheOddsApiPayloadError("market.outcomes must be a list")

                for outcome_index, raw_outcome in enumerate(outcomes):
                    if not isinstance(raw_outcome, dict):
                        raise TheOddsApiPayloadError(
                            f"market.outcomes[{outcome_index}] must be an object"
                        )
                    outcome_name = _plain_text(
                        raw_outcome.get("name"), "outcome.name"
                    )
                    description = _optional_provider_text(
                        raw_outcome.get("description"), "outcome.description"
                    )
                    outcome_sid = _optional_provider_text(
                        raw_outcome.get("sid"), "outcome.sid"
                    )
                    decimal_odds = _decimal(
                        raw_outcome.get("price"),
                        "outcome.price",
                        greater_than_one=True,
                    )
                    raw_point = raw_outcome.get("point")
                    point = (
                        None
                        if raw_point is None
                        else _decimal(raw_point, "outcome.point")
                    )
                    if market_key in {"spreads", "totals"} and point is None:
                        raise TheOddsApiPayloadError(
                            f"{market_key} outcome requires an exact point"
                        )
                    raw_bet_limit = raw_outcome.get("bet_limit")
                    bet_limit = (
                        None
                        if raw_bet_limit is None
                        else _decimal(
                            raw_bet_limit,
                            "outcome.bet_limit",
                            nonnegative=True,
                        )
                    )
                    side = "lay" if market_key.endswith("_lay") else None
                    point_text = None if point is None else _decimal_text(point)
                    line_identity = (
                        None if point is None else _decimal_text(abs(point))
                    )
                    exact_identity = (
                        event_id,
                        event_sport,
                        bookmaker_key,
                        bookmaker_event_sid,
                        market_key,
                        market_sid,
                        outcome_name,
                        description,
                        point_text,
                        outcome_sid,
                        side,
                    )
                    if exact_identity in seen:
                        raise TheOddsApiPayloadError(
                            "duplicate exact provider quote identity"
                        )
                    seen.add(exact_identity)

                    market_identity = _identity_digest(
                        bookmaker_key,
                        market_key,
                        market_sid,
                        description,
                        line_identity,
                        side,
                    )
                    selection_identity = _identity_digest(
                        market_identity,
                        outcome_name,
                        description,
                        point_text,
                        outcome_sid,
                        side,
                    )
                    sequence = self._sequence(
                        source_ts,
                        historical_snapshot_at,
                    )
                    metadata = {
                        "provider": "the_odds_api",
                        "provider_docs_ref": THE_ODDS_API_DOCS_SOURCE_REF,
                        "aggregator_event_id": event_id,
                        "bookmaker_key": bookmaker_key,
                        "bookmaker_event_sid": bookmaker_event_sid,
                        "market_key": market_key,
                        "market_sid": market_sid,
                        "outcome_name": outcome_name,
                        "outcome_description": description,
                        "outcome_sid": outcome_sid,
                        "point": point_text,
                        "bet_limit": (
                            None if bet_limit is None else _decimal_text(bet_limit)
                        ),
                        "exchange_side": side,
                        "commence_time": commence_time,
                        "historical_snapshot_at": historical_snapshot_at,
                        "coverage_complete": False,
                        "request": evidence.source_metadata(),
                        "terms": {
                            "source_ref": THE_ODDS_API_TERMS_SOURCE_REF,
                            "last_updated": THE_ODDS_API_TERMS_LAST_UPDATED,
                            "provider_stated_analytics_research_training_permitted": True,
                            "provider_stated_standalone_raw_redistribution_permitted": False,
                            "provider_stated_wagering_operator": False,
                            "lawful_use_authority": False,
                        },
                    }
                    yield ProviderQuote(
                        provider_event_id=event_id,
                        provider_market_id=(
                            f"{bookmaker_key}:{market_key}:{market_identity[:24]}"
                        ),
                        provider_selection_id=selection_identity,
                        decimal_odds=decimal_odds,
                        observed_ts=observed_at,
                        sequence=sequence,
                        market_type=_market_type(market_key),
                        status="observed",
                        source_ts=source_ts,
                        metadata=metadata,
                        sport=event_sport,
                        exchange_side=side,
                    )

    def _validate_freshness(
        self,
        source_ts: str,
        reference_at: str,
        *,
        reference_field: str,
    ) -> None:
        source = _datetime(source_ts)
        reference = _datetime(reference_at)
        if source > reference:
            raise TheOddsApiPayloadError(
                f"market.last_update cannot be later than {reference_field} time"
            )
        if self.max_market_age_seconds is not None:
            age_seconds = (reference - source).total_seconds()
            if age_seconds > self.max_market_age_seconds:
                raise TheOddsApiPayloadError(
                    "market.last_update is stale for configured freshness bound"
                )

    @staticmethod
    def _sequence(
        source_ts: str,
        historical_snapshot_at: str | None,
    ) -> int:
        if historical_snapshot_at is not None:
            return _chronological_microseconds(
                historical_snapshot_at,
                "historical_snapshot_at",
            )
        return _chronological_microseconds(source_ts, "market.last_update")