from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .domain import MarketType, utc_now_iso
from .providers import ProviderBatch, ProviderQuote, ProviderUnavailableError
from .provider_sequence_authority import SQLiteProviderSequenceAuthority


_SANDBOX_BASE_URL = "https://api.sandbox.prophetx.dev/partner"
_MARKETS_PATH = "/v3/affiliate/get_markets"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AMERICAN_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_SEQUENCE_SOURCE_ID = "prophetx:sandbox:rest:v3-affiliate-get-markets"


class ProphetXPayloadError(ValueError):
    """Provider payload cannot be projected without inventing market truth."""


class ProphetXTransportError(ProviderUnavailableError):
    """Read-only ProphetX transport failure before canonical publication."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ProphetXJsonResponse:
    payload: Any
    status_code: int
    headers: Mapping[str, str]
    body_sha256: str

    def __post_init__(self) -> None:
        if type(self.status_code) is not int:
            raise TypeError("status_code must be a non-boolean int")
        if not isinstance(self.headers, Mapping):
            raise TypeError("headers must be a mapping")
        if not isinstance(self.body_sha256, str) or not _SHA256_RE.fullmatch(self.body_sha256):
            raise ValueError("body_sha256 must be lowercase SHA-256 hex")


Transport = Callable[[str, Mapping[str, str], float], ProphetXJsonResponse]
Clock = Callable[[], str]
_INTERNAL_TRANSPORT = object()


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise HTTPError(req.full_url, code, "provider redirect refused", headers, fp)


def _decode_provider_json(raw: bytes) -> Any:
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ProphetXPayloadError("provider response exceeds maximum size")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProphetXPayloadError("provider returned invalid UTF-8 JSON") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ProphetXPayloadError(f"provider returned duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_non_finite(value: str) -> None:
        raise ProphetXPayloadError(f"provider returned non-standard JSON constant {value!r}")

    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_float=Decimal,
            parse_constant=reject_non_finite,
        )
    except json.JSONDecodeError as exc:
        raise ProphetXPayloadError("provider returned invalid JSON") from exc


def _default_transport(
    url: str,
    headers: Mapping[str, str],
    timeout: float,
) -> ProphetXJsonResponse:
    if not url.startswith(f"{_SANDBOX_BASE_URL}/"):
        raise ProphetXTransportError("provider origin is not the fixed ProphetX sandbox origin")
    request = Request(url, headers=dict(headers), method="GET")
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type", "")).lower()
            if not content_type.startswith("application/json"):
                raise ProphetXPayloadError("provider response content type is not JSON")
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ProphetXPayloadError("provider response exceeds maximum size")
            payload = _decode_provider_json(raw)
            return ProphetXJsonResponse(
                payload=payload,
                status_code=int(response.status),
                headers=dict(response.headers.items()),
                body_sha256=hashlib.sha256(raw).hexdigest(),
            )
    except HTTPError as exc:
        raise ProphetXTransportError(f"provider HTTP {int(exc.code)}", int(exc.code)) from exc
    except (URLError, TimeoutError) as exc:
        raise ProphetXTransportError("provider transport unavailable") from exc


def _positive_event_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("event_ids must contain positive non-boolean integers")
    return value


def _token(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("bearer_token must be a non-empty trimmed string")
    if "\r" in value or "\n" in value:
        raise ValueError("bearer_token must not contain control line breaks")
    return value


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    numeric = float(value)
    if not (numeric > 0 and numeric < float("inf")):
        raise ValueError("timeout_seconds must be a positive finite number")
    return numeric


def _identity(value: object, *, field: str, allow_colon: bool = True) -> str:
    if isinstance(value, bool):
        raise ProphetXPayloadError(f"{field} must be a provider identity")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProphetXPayloadError(f"{field} must be a non-empty trimmed provider identity")
    if "|" in value:
        raise ProphetXPayloadError(f"{field} must not contain reserved identity delimiter '|'")
    if not allow_colon and ":" in value:
        raise ProphetXPayloadError(
            f"{field} must not contain reserved source-scope delimiter ':'"
        )
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProphetXPayloadError(f"{field} must be a non-empty trimmed string when present")
    return value


def _decimal_quantity(value: object) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProphetXPayloadError("liquidity quantity must use exact decimal ingress")
    if not isinstance(value, (str, int, Decimal)):
        raise ProphetXPayloadError("liquidity quantity must be decimal-compatible")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProphetXPayloadError("liquidity quantity must be finite and non-negative") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ProphetXPayloadError("liquidity quantity must be finite and non-negative")
    return parsed


def _american_integer(value: object) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProphetXPayloadError("American odds must use exact integer ingress")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ProphetXPayloadError("American odds must be an exact integer")
        value = int(value)
    elif isinstance(value, str):
        if not value or value != value.strip() or value.startswith("+") and len(value) == 1:
            raise ProphetXPayloadError("American odds must be an exact integer")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ProphetXPayloadError("American odds must be an exact integer") from exc
        if not parsed.is_finite() or parsed != parsed.to_integral_value():
            raise ProphetXPayloadError("American odds must be an exact integer")
        value = int(parsed)
    if type(value) is not int or value == 0 or abs(value) < 100:
        raise ProphetXPayloadError("American odds absolute value must be at least 100")
    return value


def american_to_decimal(value: object) -> Decimal:
    """Convert exact integer American odds to deterministic Decimal odds.

    Negative American odds are rational/repeating for many prices, so Autosport uses
    a fixed 34-digit decimal context instead of binary float or ambient Decimal context.
    """

    american = _american_integer(value)
    with localcontext(_AMERICAN_CONTEXT):
        if american > 0:
            return +(Decimal(1) + (Decimal(american) / Decimal(100)))
        return +(Decimal(1) + (Decimal(100) / Decimal(abs(american))))


def _canonical_decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ProphetXPayloadError("decimal value must be finite")
    return str(value)


def _observed_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProphetXPayloadError("product receive timestamp must be canonical ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProphetXPayloadError("product receive timestamp must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProphetXPayloadError("product receive timestamp must be timezone-aware")
    return value


def _market_type(value: str) -> MarketType:
    normalized = value.lower()
    if normalized in {"moneyline", "sup_moneyline", "moneyline_3_way"}:
        return MarketType.WINNER
    if normalized == "spread":
        return MarketType.HANDICAP
    if normalized == "total":
        return MarketType.TOTAL
    return MarketType.OTHER


def _market_status(raw: object) -> tuple[str, str | None]:
    if raw is None:
        return "unknown", None
    text = _optional_text(raw, field="market status")
    assert text is not None
    normalized = text.lower()
    if normalized in {"open", "suspended", "closed"}:
        return normalized, text
    return "unknown", text


def _extract_markets(payload: Any, *, event_id: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or "data" not in payload:
        raise ProphetXPayloadError("ProphetX market response requires a data field")
    data = payload["data"]
    markets: object
    if isinstance(data, list):
        markets = data
    elif isinstance(data, dict) and isinstance(data.get("markets"), list):
        markets = data["markets"]
    elif isinstance(data, dict) and isinstance(data.get(str(event_id)), list):
        markets = data[str(event_id)]
    else:
        raise ProphetXPayloadError("ProphetX market response data has unsupported shape")
    if not isinstance(markets, list) or not all(isinstance(item, dict) for item in markets):
        raise ProphetXPayloadError("ProphetX markets must be a list of objects")
    return markets


def _market_id(market: Mapping[str, Any]) -> str:
    candidates = [market.get("market_id"), market.get("id")]
    present = [value for value in candidates if value is not None]
    if not present:
        raise ProphetXPayloadError("market is missing market_id")
    normalized = [_identity(value, field="market_id") for value in present]
    if len(set(normalized)) != 1:
        raise ProphetXPayloadError("market_id and id disagree")
    return normalized[0]


def _selection_levels(raw: object) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list) and raw and all(isinstance(item, dict) for item in raw):
        return raw
    raise ProphetXPayloadError("selection liquidity must be an object or non-empty list of objects")


def _selection_identity(levels: list[dict[str, Any]], *, field: str) -> str | None:
    values = [level.get(field) for level in levels if level.get(field) is not None]
    if not values:
        return None
    normalized = [_identity(value, field=field) for value in values]
    if len(set(normalized)) != 1:
        raise ProphetXPayloadError(f"selection liquidity levels disagree on {field}")
    return normalized[0]


def _selection_text(levels: list[dict[str, Any]], *, field: str) -> str | None:
    values = [level.get(field) for level in levels if level.get(field) is not None]
    if not values:
        return None
    normalized = [_optional_text(value, field=field) for value in values]
    if len(set(normalized)) != 1:
        raise ProphetXPayloadError(f"selection liquidity levels disagree on {field}")
    return normalized[0]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    quotes: tuple[ProviderQuote, ...]
    cursor: str
    quality_flags: tuple[str, ...]


class ProphetXRestMarketProvider:
    """Strictly read-only ProphetX sandbox REST market-data adapter.

    The provider intentionally exposes no order, account or cancellation methods. It
    consumes event-scoped v3 Market Data API snapshots only. Real-time Talaria gap/
    reconnect authority is a separate product lineage and is not simulated here.
    """

    source_id = "prophetx:sandbox"

    def __init__(
        self,
        bearer_token: str,
        event_ids: tuple[int, ...],
        *,
        sequence_authority: SQLiteProviderSequenceAuthority,
        timeout_seconds: float = 10.0,
        transport: Transport | object = _INTERNAL_TRANSPORT,
        clock: Clock = utc_now_iso,
    ) -> None:
        self._bearer_token = _token(bearer_token)
        if type(event_ids) is not tuple or not event_ids:
            raise ValueError("event_ids must be a non-empty tuple")
        validated = tuple(_positive_event_id(value) for value in event_ids)
        if len(set(validated)) != len(validated):
            raise ValueError("event_ids must not contain duplicates")
        self.event_ids = validated
        self.timeout_seconds = _positive_timeout(timeout_seconds)
        if type(sequence_authority) is not SQLiteProviderSequenceAuthority:
            raise TypeError(
                "sequence_authority must be exact SQLiteProviderSequenceAuthority"
            )
        self._sequence_authority = sequence_authority
        if transport is _INTERNAL_TRANSPORT:
            self.transport: Transport = _default_transport
            self._provider_origin_transport: Transport | None = self.transport
        else:
            if not callable(transport):
                raise TypeError("transport must be callable")
            self.transport = transport
            self._provider_origin_transport = None
        self.clock = clock
        self._pending: _Snapshot | None = None
        self._offset = 0

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be a positive non-boolean integer")
        if self._pending is None:
            self._pending = self._acquire_snapshot()
            self._offset = 0

        snapshot = self._pending
        start = self._offset
        end = min(start + max_items, len(snapshot.quotes))
        quotes = snapshot.quotes[start:end]
        self._offset = end

        if end < len(snapshot.quotes):
            flags = (*snapshot.quality_flags, "TRUNCATED_BATCH")
        else:
            flags = snapshot.quality_flags
            self._pending = None
            self._offset = 0

        return ProviderBatch(
            source_id=self.source_id,
            quotes=quotes,
            cursor=snapshot.cursor,
            quality_flags=flags,
        )

    def _acquire_snapshot(self) -> _Snapshot:
        observed_ts = _observed_timestamp(self.clock())
        all_quotes: list[ProviderQuote] = []
        response_bindings: list[tuple[int, str]] = []
        empty_markets = False
        seen_quote_keys: set[tuple[str, str, str]] = set()

        responses: list[tuple[int, ProphetXJsonResponse, bool]] = []
        for event_id in self.event_ids:
            response, provider_origin_verified = self._fetch_event(event_id)
            responses.append((event_id, response, provider_origin_verified))

        # Build and validate the complete snapshot before consuming a durable
        # acquisition sequence. Sequence 1 is a private, non-published validation
        # placeholder only; every returned ProviderQuote is replaced below.
        all_origins_verified = True
        for event_id, response, provider_origin_verified in responses:
            all_origins_verified = all_origins_verified and provider_origin_verified
            response_bindings.append((event_id, response.body_sha256))
            markets = _extract_markets(response.payload, event_id=event_id)
            if not markets:
                empty_markets = True
            quotes, had_empty = self._project_event_markets(
                event_id=event_id,
                markets=markets,
                observed_ts=observed_ts,
                sequence=1,
                response_sha256=response.body_sha256,
                provider_origin_verified=provider_origin_verified,
            )
            empty_markets = empty_markets or had_empty
            for quote in quotes:
                key = (
                    quote.provider_event_id,
                    quote.provider_market_id,
                    quote.provider_selection_id,
                )
                if key in seen_quote_keys:
                    raise ProphetXPayloadError(
                        "provider snapshot contains duplicate event/market/strike identity"
                    )
                seen_quote_keys.add(key)
                all_quotes.append(quote)

        request_binding = {
            "schema": "autosport.prophetx-rest-request-binding.v1",
            "source_id": self.source_id,
            "sequence_source_id": _SEQUENCE_SOURCE_ID,
            "transport_surface": "v3_affiliate_get_markets",
            "event_ids": list(self.event_ids),
            "get_all_market": True,
        }
        request_bytes = json.dumps(
            request_binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        request_fingerprint_sha256 = hashlib.sha256(request_bytes).hexdigest()

        snapshot_binding = {
            "schema": "autosport.prophetx-rest-snapshot-binding.v1",
            "request_fingerprint_sha256": request_fingerprint_sha256,
            "responses": [[event_id, digest] for event_id, digest in response_bindings],
        }
        snapshot_bytes = json.dumps(
            snapshot_binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        snapshot_fingerprint_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()

        sequence = self._sequence_authority(_SEQUENCE_SOURCE_ID)
        if type(sequence) is not int or sequence <= 0 or sequence > (1 << 63) - 1:
            raise ProphetXPayloadError(
                "provider sequence authority returned non-canonical sequence"
            )

        published_quotes = tuple(
            replace(
                quote,
                sequence=sequence,
                metadata={
                    **quote.metadata,
                    "product_acquisition_sequence": sequence,
                    "sequence_authority_id": self._sequence_authority.authority_id,
                    "sequence_source_id": _SEQUENCE_SOURCE_ID,
                    "request_fingerprint_sha256": request_fingerprint_sha256,
                    "snapshot_fingerprint_sha256": snapshot_fingerprint_sha256,
                },
            )
            for quote in all_quotes
        )

        flags = [
            "PROPHETX_REST_SNAPSHOT",
            "BOUNDED_PROVIDER_DEPTH",
            "PRODUCT_ACQUISITION_SEQUENCE_AUTHORITY",
        ]
        if all_origins_verified:
            flags.append("PROVIDER_ORIGIN_VERIFIED")
        else:
            flags.append("UNVERIFIED_PROVIDER_ORIGIN")
        if empty_markets and all_origins_verified:
            flags.append("PROVIDER_DECLARED_EMPTY_MARKET")
        return _Snapshot(
            quotes=published_quotes,
            cursor=(
                f"acquisition:{sequence}:"
                f"sha256:{snapshot_fingerprint_sha256}"
            ),
            quality_flags=tuple(flags),
        )

    def _fetch_event(self, event_id: int) -> tuple[ProphetXJsonResponse, bool]:
        query = urlencode({"event_id": event_id, "get_all_market": "true"})
        url = f"{_SANDBOX_BASE_URL}{_MARKETS_PATH}?{query}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._bearer_token}",
            "User-Agent": "Autosport/0.1 ProphetX read-only market observer",
        }
        transport = self.transport
        provider_origin_verified = (
            self._provider_origin_transport is not None
            and transport is self._provider_origin_transport
        )
        try:
            response = transport(url, headers, self.timeout_seconds)
        except ProphetXTransportError as exc:
            raise ProphetXTransportError(
                "provider transport unavailable",
                exc.status_code,
            ) from exc
        except Exception as exc:
            raise ProphetXTransportError("provider transport unavailable") from exc
        if type(response) is not ProphetXJsonResponse:
            raise TypeError("transport must return ProphetXJsonResponse")
        if response.status_code != 200:
            raise ProphetXTransportError(
                f"provider HTTP {response.status_code}",
                response.status_code,
            )
        return response, provider_origin_verified

    def _project_event_markets(
        self,
        *,
        event_id: int,
        markets: list[dict[str, Any]],
        observed_ts: str,
        sequence: int,
        response_sha256: str,
        provider_origin_verified: bool,
    ) -> tuple[list[ProviderQuote], bool]:
        provider_event_id = _identity(event_id, field="event_id", allow_colon=False)
        quotes: list[ProviderQuote] = []
        seen_markets: set[str] = set()
        had_empty = False

        for market in markets:
            if "event_id" in market:
                bound_event = _identity(
                    market["event_id"],
                    field="market event_id",
                    allow_colon=False,
                )
                if bound_event != provider_event_id:
                    raise ProphetXPayloadError("market event_id does not match requested event")
            market_id = _market_id(market)
            if market_id in seen_markets:
                raise ProphetXPayloadError("provider response contains duplicate market_id")
            seen_markets.add(market_id)

            raw_type = _optional_text(market.get("type"), field="market type")
            if raw_type is None:
                raise ProphetXPayloadError("market is missing type")
            subtype = _optional_text(market.get("sub_type"), field="market sub_type")
            status, raw_status = _market_status(market.get("status"))
            selections = market.get("selections")
            if not isinstance(selections, list):
                raise ProphetXPayloadError("market selections must be a list")
            if not selections:
                had_empty = True
                continue

            seen_strikes: set[str] = set()
            for raw_selection in selections:
                levels = _selection_levels(raw_selection)
                strike_id = _selection_identity(levels, field="strike_id")
                if strike_id is None:
                    raise ProphetXPayloadError("selection is missing strike_id")
                if strike_id in seen_strikes:
                    raise ProphetXPayloadError("market contains duplicate strike_id")
                seen_strikes.add(strike_id)

                outcome_id = _selection_identity(levels, field="outcome_id")
                competitor_id = _selection_identity(levels, field="competitor_id")
                selection_name = _selection_text(levels, field="name")
                depth: list[dict[str, Any]] = []
                seen_prices: set[int] = set()
                for level in levels:
                    if "price" not in level or "quantity" not in level:
                        raise ProphetXPayloadError(
                            "selection liquidity level requires price and quantity"
                        )
                    american = _american_integer(level["price"])
                    if american in seen_prices:
                        raise ProphetXPayloadError(
                            "selection liquidity contains duplicate price level"
                        )
                    seen_prices.add(american)
                    decimal_odds = american_to_decimal(american)
                    quantity = _decimal_quantity(level["quantity"])
                    depth.append(
                        {
                            "american_odds": str(american),
                            "decimal_odds": _canonical_decimal_text(decimal_odds),
                            "quantity": _canonical_decimal_text(quantity),
                        }
                    )

                primary = depth[0]
                quotes.append(
                    ProviderQuote(
                        provider_event_id=provider_event_id,
                        provider_market_id=market_id,
                        provider_selection_id=strike_id,
                        decimal_odds=Decimal(primary["decimal_odds"]),
                        observed_ts=observed_ts,
                        sequence=sequence,
                        market_type=_market_type(raw_type),
                        status=status,
                        source_ts=None,
                        metadata={
                            "provider": "prophetx",
                            "environment": "sandbox",
                            "market_type_raw": raw_type,
                            "market_sub_type": subtype,
                            "market_status_raw": raw_status,
                            "strike_id": strike_id,
                            "outcome_id": outcome_id,
                            "competitor_id": competitor_id,
                            "selection_name": selection_name,
                            "depth_levels": depth,
                            "depth_level_count": len(depth),
                            "primary_level_index": 0,
                            "primary_level_semantics": "provider_order_level_0",
                            "depth_is_aggregated": True,
                            "depth_completeness": "bounded_provider_response",
                            "execution_fillability_proven": False,
                            "response_sha256": response_sha256,
                            "provider_source_timestamp_available": False,
                            "provider_origin_verified": provider_origin_verified,
                            "provider_origin_authority": (
                                "fixed_sandbox_https"
                                if provider_origin_verified
                                else "unverified_injected_transport"
                            ),
                            "transport_surface": "v3_affiliate_get_markets",
                        },
                    )
                )
        return quotes, had_empty
