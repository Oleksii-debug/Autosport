"""Strict read-only Betfair Exchange account JSON-RPC client.

This adapter exposes only authenticated account/details/current-order/cleared-order reads.
Credentials are memory-only; write RPCs are not reachable through the client. Provider-native
numbers remain Decimal observations and every response carries an exact SHA-256 evidence hash.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from threading import Lock
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ACCOUNT_JSON_RPC_ENDPOINT = "https://api.betfair.com/exchange/account/json-rpc/v1"
BETTING_JSON_RPC_ENDPOINT = "https://api.betfair.com/exchange/betting/json-rpc/v1"
ADAPTER_ID = "betfair-exchange-jsonrpc-readonly"
ADAPTER_VERSION = "1"
_GET_ACCOUNT_FUNDS = "AccountAPING/v1.0/getAccountFunds"
_GET_ACCOUNT_DETAILS = "AccountAPING/v1.0/getAccountDetails"
_LIST_CURRENT_ORDERS = "SportsAPING/v1.0/listCurrentOrders"
_LIST_CLEARED_ORDERS = "SportsAPING/v1.0/listClearedOrders"
_READ_METHOD_ENDPOINT = MappingProxyType({
    _GET_ACCOUNT_FUNDS: ACCOUNT_JSON_RPC_ENDPOINT,
    _GET_ACCOUNT_DETAILS: ACCOUNT_JSON_RPC_ENDPOINT,
    _LIST_CURRENT_ORDERS: BETTING_JSON_RPC_ENDPOINT,
    _LIST_CLEARED_ORDERS: BETTING_JSON_RPC_ENDPOINT,
})


class BetfairReadOnlyError(RuntimeError):
    """Raised when provider read evidence cannot be accepted safely."""


@dataclass(frozen=True, slots=True, repr=False)
class BetfairSessionCredentials:
    application_key: str
    session_token: str

    def __post_init__(self) -> None:
        _required_text(self.application_key, "application_key")
        _required_text(self.session_token, "session_token")

    def __repr__(self) -> str:
        return "BetfairSessionCredentials(application_key=<redacted>, session_token=<redacted>)"


class BetfairHttpTransport(Protocol):
    def post(self, url: str, *, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> bytes: ...


class UrllibBetfairHttpTransport:
    def __init__(self, *, max_response_bytes: int = 8 * 1024 * 1024) -> None:
        if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool) or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._max_response_bytes = max_response_bytes

    def post(self, url: str, *, headers: Mapping[str, str], body: bytes, timeout_seconds: float) -> bytes:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(self._max_response_bytes + 1)
        except HTTPError as exc:
            raise BetfairReadOnlyError(f"Betfair HTTP request failed with status {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise BetfairReadOnlyError("Betfair network request failed") from None
        if len(payload) > self._max_response_bytes:
            raise BetfairReadOnlyError("Betfair response exceeded the size limit")
        return payload


@dataclass(frozen=True, slots=True)
class BetfairEvidence:
    observed_at: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        _iso_timestamp(self.observed_at, "observed_at")
        _sha256_hex(self.source_payload_sha256, "source_payload_sha256")


@dataclass(frozen=True, slots=True)
class BetfairAccountFundsObservation:
    available_to_bet_balance: Decimal
    exposure: Decimal
    retained_commission: Decimal
    exposure_limit: Decimal
    evidence: BetfairEvidence

    def __post_init__(self) -> None:
        _decimal(self.available_to_bet_balance, "available_to_bet_balance")
        _decimal(self.exposure, "exposure")
        _decimal(self.retained_commission, "retained_commission")
        _decimal(self.exposure_limit, "exposure_limit")


@dataclass(frozen=True, slots=True)
class BetfairAccountDetailsObservation:
    currency_code: str
    locale_code: str | None
    region: str | None
    timezone_name: str | None
    evidence: BetfairEvidence

    def __post_init__(self) -> None:
        code = _required_text(self.currency_code, "currency_code")
        if code != code.upper() or not code.isascii():
            raise BetfairReadOnlyError("currency_code must be uppercase ASCII provider currency code")
        _optional_text(self.locale_code, "locale_code")
        _optional_text(self.region, "region")
        _optional_text(self.timezone_name, "timezone_name")


@dataclass(frozen=True, slots=True)
class BetfairCurrentOrderObservation:
    bet_id: str
    market_id: str
    selection_id: int
    side: str
    status: str
    placed_date: str
    price: Decimal | None
    requested_size: Decimal | None
    average_price_matched: Decimal
    size_matched: Decimal
    size_remaining: Decimal
    customer_order_ref: str | None
    customer_strategy_ref: str | None
    evidence: BetfairEvidence

    def __post_init__(self) -> None:
        _required_text(self.bet_id, "bet_id")
        _required_text(self.market_id, "market_id")
        _positive_int(self.selection_id, "selection_id")
        _enum_text(self.side, "side", {"BACK", "LAY"})
        _required_text(self.status, "status")
        _iso_timestamp(self.placed_date, "placed_date")
        if self.price is not None:
            _positive_decimal(self.price, "price")
        if self.requested_size is not None:
            _nonnegative_decimal(self.requested_size, "requested_size")
        _nonnegative_decimal(self.average_price_matched, "average_price_matched")
        _nonnegative_decimal(self.size_matched, "size_matched")
        _nonnegative_decimal(self.size_remaining, "size_remaining")
        _optional_text(self.customer_order_ref, "customer_order_ref")
        _optional_text(self.customer_strategy_ref, "customer_strategy_ref")


@dataclass(frozen=True, slots=True)
class BetfairClearedOrderObservation:
    bet_id: str
    market_id: str
    selection_id: int
    side: str
    bet_status: str
    placed_date: str
    settled_date: str
    price_requested: Decimal
    price_matched: Decimal
    size_settled: Decimal
    profit: Decimal
    customer_order_ref: str | None
    customer_strategy_ref: str | None
    evidence: BetfairEvidence

    def __post_init__(self) -> None:
        _required_text(self.bet_id, "bet_id")
        _required_text(self.market_id, "market_id")
        _positive_int(self.selection_id, "selection_id")
        _enum_text(self.side, "side", {"BACK", "LAY"})
        _required_text(self.bet_status, "bet_status")
        _iso_timestamp(self.placed_date, "placed_date")
        _iso_timestamp(self.settled_date, "settled_date")
        _positive_decimal(self.price_requested, "price_requested")
        _positive_decimal(self.price_matched, "price_matched")
        _nonnegative_decimal(self.size_settled, "size_settled")
        _decimal(self.profit, "profit")
        _optional_text(self.customer_order_ref, "customer_order_ref")
        _optional_text(self.customer_strategy_ref, "customer_strategy_ref")


@dataclass(frozen=True, slots=True)
class BetfairCurrentOrderPage:
    orders: tuple[BetfairCurrentOrderObservation, ...]
    more_available: bool
    from_record: int
    record_count: int
    evidence: BetfairEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.orders, tuple) or not isinstance(self.more_available, bool):
            raise BetfairReadOnlyError("invalid current order page")
        _nonnegative_int(self.from_record, "from_record")
        _positive_int(self.record_count, "record_count")


@dataclass(frozen=True, slots=True)
class BetfairClearedOrderPage:
    orders: tuple[BetfairClearedOrderObservation, ...]
    more_available: bool
    from_record: int
    record_count: int
    evidence: BetfairEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.orders, tuple) or not isinstance(self.more_available, bool):
            raise BetfairReadOnlyError("invalid cleared order page")
        _nonnegative_int(self.from_record, "from_record")
        _positive_int(self.record_count, "record_count")


@dataclass(frozen=True, slots=True)
class _RpcResult:
    result: object
    evidence: BetfairEvidence


class BetfairReadOnlyClient:
    def __init__(self, credentials: BetfairSessionCredentials, *, transport: BetfairHttpTransport | None = None, timeout_seconds: float = 10.0, clock: Callable[[], datetime] | None = None, venue_id: str = "betfair", account_id: str = "default-account") -> None:
        if not isinstance(credentials, BetfairSessionCredentials):
            raise TypeError("credentials must be BetfairSessionCredentials")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._credentials = credentials
        self._transport = transport or UrllibBetfairHttpTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._request_id = 0
        self._request_lock = Lock()
        self._venue_id = _required_text(venue_id, "venue_id")
        self._account_id = _required_text(account_id, "account_id")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(adapter_id={ADAPTER_ID!r}, adapter_version={ADAPTER_VERSION!r})"

    def read_account_funds(self) -> BetfairAccountFundsObservation:
        response = self._rpc(_GET_ACCOUNT_FUNDS, {})
        result = _mapping(response.result, "getAccountFunds result")
        return BetfairAccountFundsObservation(
            _number(result, "availableToBetBalance", "available_to_bet_balance"),
            _number(result, "exposure", "exposure"),
            _number(result, "retainedCommission", "retained_commission"),
            _number(result, "exposureLimit", "exposure_limit"),
            response.evidence,
        )

    def read_account_details(self) -> BetfairAccountDetailsObservation:
        response = self._rpc(_GET_ACCOUNT_DETAILS, {})
        result = _mapping(response.result, "getAccountDetails result")
        return BetfairAccountDetailsObservation(
            _provider_text(result, "currencyCode", "currency_code"),
            _provider_optional_text(result, "localeCode", "locale_code"),
            _provider_optional_text(result, "region", "region"),
            _provider_optional_text(result, "timezone", "timezone"),
            response.evidence,
        )

    def read_current_orders_page(self, *, from_record: int = 0, record_count: int = 1000) -> BetfairCurrentOrderPage:
        _page_bounds(from_record, record_count)
        response = self._rpc(_LIST_CURRENT_ORDERS, {"orderProjection": "ALL", "fromRecord": from_record, "recordCount": record_count})
        report = _mapping(response.result, "listCurrentOrders result")
        raw_orders = _sequence(report.get("currentOrders"), "currentOrders")
        orders = tuple(_parse_current_order(raw, response.evidence, i) for i, raw in enumerate(raw_orders))
        _unique_bet_ids(orders, "currentOrders")
        return BetfairCurrentOrderPage(orders, _provider_bool(report, "moreAvailable"), from_record, record_count, response.evidence)

    def read_cleared_orders_page(self, *, from_record: int = 0, record_count: int = 1000, settled_from: str | None = None) -> BetfairClearedOrderPage:
        _page_bounds(from_record, record_count)
        params: dict[str, object] = {"betStatus": "SETTLED", "groupBy": "BET", "fromRecord": from_record, "recordCount": record_count}
        if settled_from is not None:
            _iso_timestamp(settled_from, "settled_from")
            params["settledDateRange"] = {"from": settled_from}
        response = self._rpc(_LIST_CLEARED_ORDERS, params)
        report = _mapping(response.result, "listClearedOrders result")
        raw_orders = _sequence(report.get("clearedOrders"), "clearedOrders")
        orders = tuple(_parse_cleared_order(raw, response.evidence, i) for i, raw in enumerate(raw_orders))
        _unique_bet_ids(orders, "clearedOrders")
        return BetfairClearedOrderPage(orders, _provider_bool(report, "moreAvailable"), from_record, record_count, response.evidence)

    def read_all_current_orders(self, *, page_size: int = 1000, max_pages: int = 100) -> tuple[BetfairCurrentOrderObservation, ...]:
        _positive_int(max_pages, "max_pages")
        _page_bounds(0, page_size)
        offset, result, seen = 0, [], set()
        for _ in range(max_pages):
            page = self.read_current_orders_page(from_record=offset, record_count=page_size)
            _extend_unique(result, seen, page.orders, "currentOrders")
            if not page.more_available:
                return tuple(result)
            if not page.orders:
                raise BetfairReadOnlyError("currentOrders reported moreAvailable with an empty page")
            offset += len(page.orders)
        raise BetfairReadOnlyError("currentOrders pagination exceeded max_pages while more data remained")

    def read_all_cleared_orders(self, *, settled_from: str | None = None, page_size: int = 1000, max_pages: int = 100) -> tuple[BetfairClearedOrderObservation, ...]:
        _positive_int(max_pages, "max_pages")
        _page_bounds(0, page_size)
        offset, result, seen = 0, [], set()
        for _ in range(max_pages):
            page = self.read_cleared_orders_page(from_record=offset, record_count=page_size, settled_from=settled_from)
            _extend_unique(result, seen, page.orders, "clearedOrders")
            if not page.more_available:
                return tuple(result)
            if not page.orders:
                raise BetfairReadOnlyError("clearedOrders reported moreAvailable with an empty page")
            offset += len(page.orders)
        raise BetfairReadOnlyError("clearedOrders pagination exceeded max_pages while more data remained")

    def read_account_snapshot(self, requested_capabilities: frozenset) -> object:
        from .bookmaker_capability import (
            BookmakerAccountSnapshot,
            BookmakerBalanceObservation,
            BookmakerCapability,
            BookmakerCapabilityFact,
            BookmakerCapabilityProfile,
            BookmakerCapabilityState,
            BookmakerPositionObservation,
            BookmakerPositionState,
        )
        if not isinstance(requested_capabilities, frozenset):
            raise TypeError("requested_capabilities must be a frozenset")
        supported = {BookmakerCapability.BALANCE_READ, BookmakerCapability.OPEN_POSITIONS_READ, BookmakerCapability.SETTLED_POSITIONS_READ}
        if any(c not in supported for c in requested_capabilities):
            raise BetfairReadOnlyError("requested capability is not implemented by the Betfair account adapter")
        details = self.read_account_details()
        funds = self.read_account_funds() if BookmakerCapability.BALANCE_READ in requested_capabilities else None
        current = self.read_all_current_orders() if BookmakerCapability.OPEN_POSITIONS_READ in requested_capabilities else ()
        cleared = self.read_all_cleared_orders() if BookmakerCapability.SETTLED_POSITIONS_READ in requested_capabilities else ()
        common_caps = frozenset(requested_capabilities)
        facts = tuple(BookmakerCapabilityFact(c, BookmakerCapabilityState.SUPPORTED) for c in sorted(common_caps, key=lambda x: x.value))
        hashes = [details.evidence.source_payload_sha256]
        if funds is not None:
            hashes.append(funds.evidence.source_payload_sha256)
        hashes.extend(o.evidence.source_payload_sha256 for o in current)
        hashes.extend(o.evidence.source_payload_sha256 for o in cleared)
        evidence_hash = sha256("|".join(hashes).encode("ascii")).hexdigest()
        profile = BookmakerCapabilityProfile(self._venue_id, self._account_id, ADAPTER_ID, ADAPTER_VERSION, 1, facts, details.evidence.observed_at, f"betfair://account-snapshot/{evidence_hash}", evidence_hash)
        balance = None
        if funds is not None:
            balance = BookmakerBalanceObservation(self._venue_id, self._account_id, ADAPTER_ID, f"funds:{funds.evidence.source_payload_sha256}", details.currency_code, funds.available_to_bet_balance, funds.evidence.observed_at, funds.evidence.source_payload_sha256, total_balance=None, exposure=funds.exposure, retained_commission=funds.retained_commission, exposure_limit=funds.exposure_limit)
        open_positions = tuple(self._to_position(o, BookmakerPositionState.OPEN, details.currency_code) for o in current)
        settled_positions = tuple(self._to_position(o, BookmakerPositionState.SETTLED, details.currency_code) for o in cleared)
        overlap = {p.external_position_id for p in open_positions} & {p.external_position_id for p in settled_positions}
        if overlap:
            raise BetfairReadOnlyError(f"provider returned positions in both OPEN and SETTLED states: {sorted(overlap)!r}")
        return BookmakerAccountSnapshot(profile, common_caps, self._observed_at(), balance, open_positions, settled_positions)

    def _to_position(self, order: object, state: object, currency: str) -> object:
        from .bookmaker_capability import BookmakerPositionObservation
        if isinstance(order, BetfairCurrentOrderObservation):
            amount = order.size_matched
            odds = order.average_price_matched if order.average_price_matched > 0 else order.price
            semantics = "betfair_size_matched"
        else:
            amount = order.size_settled
            odds = order.price_matched
            semantics = "betfair_size_settled"
        return BookmakerPositionObservation(self._venue_id, self._account_id, ADAPTER_ID, f"{state.value}:{order.bet_id}:{order.evidence.source_payload_sha256}", order.bet_id, state, currency, order.evidence.observed_at, order.evidence.source_payload_sha256, provider_amount=amount, provider_amount_semantics=semantics, provider_side=order.side, decimal_odds=odds)

    def _rpc(self, method: str, params: Mapping[str, object]) -> _RpcResult:
        endpoint = _READ_METHOD_ENDPOINT.get(method)
        if endpoint is None:
            raise BetfairReadOnlyError("Betfair RPC method is outside the strict read-only allowlist")
        request_id = self._next_request_id()
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": dict(params), "id": request_id}, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json", "X-Application": self._credentials.application_key, "X-Authentication": self._credentials.session_token}
        payload = self._transport.post(endpoint, headers=headers, body=body, timeout_seconds=self._timeout_seconds)
        if not isinstance(payload, bytes):
            raise BetfairReadOnlyError("Betfair transport must return bytes")
        evidence = BetfairEvidence(self._observed_at(), sha256(payload).hexdigest())
        decoded = _decode_json(payload)
        envelope = _mapping(decoded, "JSON-RPC response")
        if envelope.get("jsonrpc") != "2.0":
            raise BetfairReadOnlyError("Betfair response has invalid jsonrpc version")
        if envelope.get("id") != request_id:
            raise BetfairReadOnlyError("Betfair response id does not match request id")
        if "error" in envelope and envelope["error"] is not None:
            if "result" in envelope:
                raise BetfairReadOnlyError("Betfair response contains both error and result")
            error = envelope["error"]
            code = error.get("code") if isinstance(error, Mapping) else None
            message = error.get("message") if isinstance(error, Mapping) else None
            detail = "Betfair JSON-RPC returned an error"
            if isinstance(code, (str, int)) and not isinstance(code, bool):
                detail += f" code={code}"
            if isinstance(message, str) and message.strip():
                detail += f" message={message.strip()[:160]}"
            raise BetfairReadOnlyError(detail)
        if "result" not in envelope:
            raise BetfairReadOnlyError("Betfair response is missing result")
        return _RpcResult(envelope["result"], evidence)

    def _next_request_id(self) -> int:
        with self._request_lock:
            self._request_id += 1
            return self._request_id

    def _observed_at(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BetfairReadOnlyError("clock must return timezone-aware datetime")
        return value.isoformat()


def _decode_json(payload: bytes) -> object:
    def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise BetfairReadOnlyError(f"Betfair JSON contains duplicate object key {key!r}")
            result[key] = value
        return result
    def reject_constant(value: str) -> object:
        raise BetfairReadOnlyError(f"Betfair JSON contains non-standard numeric constant {value}")
    try:
        return json.loads(payload.decode("utf-8"), parse_float=Decimal, object_pairs_hook=reject_duplicate_pairs, parse_constant=reject_constant)
    except BetfairReadOnlyError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BetfairReadOnlyError("Betfair response is not valid UTF-8 JSON") from None


def _parse_current_order(value: object, evidence: BetfairEvidence, index: int) -> BetfairCurrentOrderObservation:
    raw = _mapping(value, f"currentOrders[{index}]")
    price_size = raw.get("priceSize")
    price = requested_size = None
    if price_size is not None:
        price_map = _mapping(price_size, f"currentOrders[{index}].priceSize")
        price = _number(price_map, "price", "price")
        requested_size = _number(price_map, "size", "size")
    return BetfairCurrentOrderObservation(_provider_text(raw, "betId", "bet_id"), _provider_text(raw, "marketId", "market_id"), _provider_int(raw, "selectionId", "selection_id"), _provider_text(raw, "side", "side"), _provider_text(raw, "status", "status"), _provider_text(raw, "placedDate", "placed_date"), price, requested_size, _number(raw, "averagePriceMatched", "average_price_matched"), _number(raw, "sizeMatched", "size_matched"), _number(raw, "sizeRemaining", "size_remaining"), _provider_optional_text(raw, "customerOrderRef", "customer_order_ref"), _provider_optional_text(raw, "customerStrategyRef", "customer_strategy_ref"), evidence)


def _parse_cleared_order(value: object, evidence: BetfairEvidence, index: int) -> BetfairClearedOrderObservation:
    raw = _mapping(value, f"clearedOrders[{index}]")
    return BetfairClearedOrderObservation(_provider_text(raw, "betId", "bet_id"), _provider_text(raw, "marketId", "market_id"), _provider_int(raw, "selectionId", "selection_id"), _provider_text(raw, "side", "side"), "SETTLED", _provider_text(raw, "placedDate", "placed_date"), _provider_text(raw, "settledDate", "settled_date"), _number(raw, "priceRequested", "price_requested"), _number(raw, "priceMatched", "price_matched"), _number(raw, "sizeSettled", "size_settled"), _number(raw, "profit", "profit"), _provider_optional_text(raw, "customerOrderRef", "customer_order_ref"), _provider_optional_text(raw, "customerStrategyRef", "customer_strategy_ref"), evidence)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise BetfairReadOnlyError(f"{field} must be a JSON object")
    return value


def _sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise BetfairReadOnlyError(f"{field} must be a JSON array")
    return value


def _provider_bool(value: Mapping[str, object], key: str) -> bool:
    raw = value.get(key)
    if not isinstance(raw, bool):
        raise BetfairReadOnlyError(f"{key} must be bool")
    return raw


def _provider_text(value: Mapping[str, object], key: str, field: str) -> str:
    if key not in value:
        raise BetfairReadOnlyError(f"{field} is missing from provider response")
    return _required_text(value[key], field)


def _provider_optional_text(value: Mapping[str, object], key: str, field: str) -> str | None:
    raw = value.get(key)
    return None if raw is None else _required_text(raw, field)


def _provider_int(value: Mapping[str, object], key: str, field: str) -> int:
    if key not in value:
        raise BetfairReadOnlyError(f"{field} is missing from provider response")
    raw = value[key]
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise BetfairReadOnlyError(f"{field} must be an integer")
    return raw


def _number(value: Mapping[str, object], key: str, field: str) -> Decimal:
    if key not in value:
        raise BetfairReadOnlyError(f"{field} is missing from provider response")
    raw = value[key]
    if isinstance(raw, Decimal):
        result = raw
    elif isinstance(raw, int) and not isinstance(raw, bool):
        result = Decimal(raw)
    else:
        raise BetfairReadOnlyError(f"{field} must be a JSON number decoded without binary float")
    return _decimal(result, field)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairReadOnlyError(f"{field} must be a non-empty trimmed string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _required_text(value, field)


def _enum_text(value: object, field: str, allowed: set[str]) -> str:
    text = _required_text(value, field)
    if text not in allowed:
        raise BetfairReadOnlyError(f"{field} must be one of {', '.join(sorted(allowed))}")
    return text


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BetfairReadOnlyError(f"{field} must be a finite Decimal")
    return value


def _positive_decimal(value: object, field: str) -> Decimal:
    result = _decimal(value, field)
    if result <= 0:
        raise BetfairReadOnlyError(f"{field} must be positive")
    return result


def _nonnegative_decimal(value: object, field: str) -> Decimal:
    result = _decimal(value, field)
    if result < 0:
        raise BetfairReadOnlyError(f"{field} must be non-negative")
    return result


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BetfairReadOnlyError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BetfairReadOnlyError(f"{field} must be a non-negative integer")
    return value


def _page_bounds(from_record: int, record_count: int) -> None:
    _nonnegative_int(from_record, "from_record")
    _positive_int(record_count, "record_count")
    if record_count > 1000:
        raise BetfairReadOnlyError("record_count cannot exceed Betfair page limit 1000")


def _iso_timestamp(value: object, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise BetfairReadOnlyError(f"{field} must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairReadOnlyError(f"{field} must include timezone offset")
    return parsed


def _sha256_hex(value: object, field: str) -> str:
    text = _required_text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise BetfairReadOnlyError(f"{field} must be lowercase 64-character SHA-256")
    return text


def _unique_bet_ids(orders: Sequence[object], field: str) -> None:
    seen: set[str] = set()
    for order in orders:
        bet_id = getattr(order, "bet_id", None)
        if not isinstance(bet_id, str):
            raise BetfairReadOnlyError(f"{field} order lacks bet_id")
        if bet_id in seen:
            raise BetfairReadOnlyError(f"{field} contains duplicate bet_id {bet_id}")
        seen.add(bet_id)


def _extend_unique(target: list[object], seen: set[str], orders: Sequence[object], field: str) -> None:
    for order in orders:
        bet_id = getattr(order, "bet_id", None)
        if not isinstance(bet_id, str):
            raise BetfairReadOnlyError(f"{field} order lacks canonical bet_id")
        if bet_id in seen:
            raise BetfairReadOnlyError(f"{field} pagination returned duplicate bet_id {bet_id}")
        seen.add(bet_id)
        target.append(order)
