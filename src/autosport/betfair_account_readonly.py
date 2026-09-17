"""Strict read-only Betfair Exchange account JSON-RPC client.

The client intentionally exposes only account/details/current-order/cleared-order reads.
It does not implement login acquisition, place/cancel/replace/update operations, or any
money-moving effect. Credentials remain memory-only and are excluded from repr/error
surfaces owned by this module.
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

_READ_METHOD_ENDPOINT = MappingProxyType(
    {
        _GET_ACCOUNT_FUNDS: ACCOUNT_JSON_RPC_ENDPOINT,
        _GET_ACCOUNT_DETAILS: ACCOUNT_JSON_RPC_ENDPOINT,
        _LIST_CURRENT_ORDERS: BETTING_JSON_RPC_ENDPOINT,
        _LIST_CLEARED_ORDERS: BETTING_JSON_RPC_ENDPOINT,
    }
)


class BetfairReadOnlyError(RuntimeError):
    """Raised when provider read evidence cannot be accepted safely."""


@dataclass(frozen=True, slots=True, repr=False)
class BetfairSessionCredentials:
    """Memory-only API headers supplied by an already authorized user session."""

    application_key: str
    session_token: str

    def __post_init__(self) -> None:
        _required_text(self.application_key, "application_key")
        _required_text(self.session_token, "session_token")

    def __repr__(self) -> str:
        return (
            "BetfairSessionCredentials("
            "application_key=<redacted>, session_token=<redacted>)"
        )


class BetfairHttpTransport(Protocol):
    """Minimal HTTP boundary used by the read-only client."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        """POST bytes and return the complete response body."""


class UrllibBetfairHttpTransport:
    """Small stdlib transport; it never logs request headers or bodies."""

    def __init__(self, *, max_response_bytes: int = 8 * 1024 * 1024) -> None:
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or max_response_bytes <= 0
        ):
            raise ValueError("max_response_bytes must be a positive integer")
        self._max_response_bytes = max_response_bytes

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read(self._max_response_bytes + 1)
        except HTTPError as exc:
            raise BetfairReadOnlyError(
                f"Betfair HTTP request failed with status {exc.code}"
            ) from None
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
    """Provider-native account funds fields; no fabricated aggregate balance."""

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
    """Non-PII account metadata needed to interpret provider monetary evidence."""

    currency_code: str
    locale_code: str | None
    region: str | None
    timezone_name: str | None
    evidence: BetfairEvidence

    def __post_init__(self) -> None:
        code = _required_text(self.currency_code, "currency_code")
        if code != code.upper() or not code.isascii():
            raise BetfairReadOnlyError(
                "currency_code must be uppercase ASCII provider currency code"
            )
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
        if not isinstance(self.orders, tuple):
            raise BetfairReadOnlyError("orders must be a tuple")
        if not isinstance(self.more_available, bool):
            raise BetfairReadOnlyError("more_available must be bool")
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
        if not isinstance(self.orders, tuple):
            raise BetfairReadOnlyError("orders must be a tuple")
        if not isinstance(self.more_available, bool):
            raise BetfairReadOnlyError("more_available must be bool")
        _nonnegative_int(self.from_record, "from_record")
        _positive_int(self.record_count, "record_count")


@dataclass(frozen=True, slots=True)
class _RpcResult:
    result: object
    evidence: BetfairEvidence


class BetfairReadOnlyClient:
    """Authenticated provider client whose public surface contains reads only."""

    def __init__(
        self,
        credentials: BetfairSessionCredentials,
        *,
        transport: BetfairHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(credentials, BetfairSessionCredentials):
            raise TypeError("credentials must be BetfairSessionCredentials")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self._credentials = credentials
        self._transport = transport or UrllibBetfairHttpTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._request_id = 0
        self._request_lock = Lock()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"adapter_id={ADAPTER_ID!r}, adapter_version={ADAPTER_VERSION!r})"
        )

    def read_account_funds(self) -> BetfairAccountFundsObservation:
        response = self._rpc(_GET_ACCOUNT_FUNDS, {})
        result = _mapping(response.result, "getAccountFunds result")
        return BetfairAccountFundsObservation(
            available_to_bet_balance=_number(
                result, "availableToBetBalance", "available_to_bet_balance"
            ),
            exposure=_number(result, "exposure", "exposure"),
            retained_commission=_number(
                result, "retainedCommission", "retained_commission"
            ),
            exposure_limit=_number(result, "exposureLimit", "exposure_limit"),
            evidence=response.evidence,
        )

    def read_account_details(self) -> BetfairAccountDetailsObservation:
        response = self._rpc(_GET_ACCOUNT_DETAILS, {})
        result = _mapping(response.result, "getAccountDetails result")
        return BetfairAccountDetailsObservation(
            currency_code=_provider_text(result, "currencyCode", "currency_code"),
            locale_code=_provider_optional_text(result, "localeCode", "locale_code"),
            region=_provider_optional_text(result, "region", "region"),
            timezone_name=_provider_optional_text(result, "timezone", "timezone"),
            evidence=response.evidence,
        )

    def read_current_orders_page(
        self,
        *,
        from_record: int = 0,
        record_count: int = 1000,
    ) -> BetfairCurrentOrderPage:
        _page_bounds(from_record, record_count)
        response = self._rpc(
            _LIST_CURRENT_ORDERS,
            {
                "orderProjection": "ALL",
                "fromRecord": from_record,
                "recordCount": record_count,
            },
        )
        report = _mapping(response.result, "listCurrentOrders result")
        raw_orders = _sequence(report.get("currentOrders"), "currentOrders")
        orders = tuple(
            _parse_current_order(raw, response.evidence, index)
            for index, raw in enumerate(raw_orders)
        )
        _unique_bet_ids(orders, "currentOrders")
        return BetfairCurrentOrderPage(
            orders=orders,
            more_available=_provider_bool(report, "moreAvailable"),
            from_record=from_record,
            record_count=record_count,
            evidence=response.evidence,
        )

    def read_cleared_orders_page(
        self,
        *,
        from_record: int = 0,
        record_count: int = 1000,
        settled_from: str | None = None,
    ) -> BetfairClearedOrderPage:
        _page_bounds(from_record, record_count)
        params: dict[str, object] = {
            "betStatus": "SETTLED",
            "groupBy": "BET",
            "fromRecord": from_record,
            "recordCount": record_count,
        }
        if settled_from is not None:
            _iso_timestamp(settled_from, "settled_from")
            params["settledDateRange"] = {"from": settled_from}
        response = self._rpc(_LIST_CLEARED_ORDERS, params)
        report = _mapping(response.result, "listClearedOrders result")
        raw_orders = _sequence(report.get("clearedOrders"), "clearedOrders")
        orders = tuple(
            _parse_cleared_order(raw, response.evidence, index)
            for index, raw in enumerate(raw_orders)
        )
        _unique_bet_ids(orders, "clearedOrders")
        return BetfairClearedOrderPage(
            orders=orders,
            more_available=_provider_bool(report, "moreAvailable"),
            from_record=from_record,
            record_count=record_count,
            evidence=response.evidence,
        )

    def read_all_current_orders(
        self,
        *,
        page_size: int = 1000,
        max_pages: int = 100,
    ) -> tuple[BetfairCurrentOrderObservation, ...]:
        return self._paginate_current(page_size=page_size, max_pages=max_pages)

    def read_all_cleared_orders(
        self,
        *,
        settled_from: str | None = None,
        page_size: int = 1000,
        max_pages: int = 100,
    ) -> tuple[BetfairClearedOrderObservation, ...]:
        _positive_int(max_pages, "max_pages")
        _page_bounds(0, page_size)
        offset = 0
        result: list[BetfairClearedOrderObservation] = []
        seen: set[str] = set()
        for _ in range(max_pages):
            page = self.read_cleared_orders_page(
                from_record=offset,
                record_count=page_size,
                settled_from=settled_from,
            )
            self._extend_unique(result, seen, page.orders, "clearedOrders")
            if not page.more_available:
                return tuple(result)
            if not page.orders:
                raise BetfairReadOnlyError(
                    "clearedOrders reported moreAvailable with an empty page"
                )
            offset += len(page.orders)
        raise BetfairReadOnlyError(
            "clearedOrders pagination exceeded max_pages while more data remained"
        )

    def _paginate_current(
        self,
        *,
        page_size: int,
        max_pages: int,
    ) -> tuple[BetfairCurrentOrderObservation, ...]:
        _positive_int(max_pages, "max_pages")
        _page_bounds(0, page_size)
        offset = 0
        result: list[BetfairCurrentOrderObservation] = []
        seen: set[str] = set()
        for _ in range(max_pages):
            page = self.read_current_orders_page(
                from_record=offset,
                record_count=page_size,
            )
            self._extend_unique(result, seen, page.orders, "currentOrders")
            if not page.more_available:
                return tuple(result)
            if not page.orders:
                raise BetfairReadOnlyError(
                    "currentOrders reported moreAvailable with an empty page"
                )
            offset += len(page.orders)
        raise BetfairReadOnlyError(
            "currentOrders pagination exceeded max_pages while more data remained"
        )

    @staticmethod
    def _extend_unique(
        target: list[object],
        seen: set[str],
        orders: Sequence[object],
        field: str,
    ) -> None:
        for order in orders:
            bet_id = getattr(order, "bet_id", None)
            if not isinstance(bet_id, str):
                raise BetfairReadOnlyError(f"{field} order lacks canonical bet_id")
            if bet_id in seen:
                raise BetfairReadOnlyError(
                    f"{field} pagination returned duplicate bet_id {bet_id}"
                )
            seen.add(bet_id)
            target.append(order)

    def _rpc(self, method: str, params: Mapping[str, object]) -> _RpcResult:
        endpoint = _READ_METHOD_ENDPOINT.get(method)
        if endpoint is None:
            raise BetfairReadOnlyError(
                "Betfair RPC method is outside the strict read-only allowlist"
            )
        request_id = self._next_request_id()
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": dict(params),
                "id": request_id,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Application": self._credentials.application_key,
            "X-Authentication": self._credentials.session_token,
        }
        payload = self._transport.post(
            endpoint,
            headers=headers,
            body=body,
            timeout_seconds=self._timeout_seconds,
        )
        if not isinstance(payload, bytes):
            raise BetfairReadOnlyError("Betfair transport must return bytes")
        evidence = BetfairEvidence(
            observed_at=self._observed_at(),
            source_payload_sha256=sha256(payload).hexdigest(),
        )
        try:
            decoded = json.loads(payload.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BetfairReadOnlyError("Betfair response is not valid UTF-8 JSON") from None
        envelope = _mapping(decoded, "JSON-RPC response")
        if envelope.get("jsonrpc") != "2.0":
            raise BetfairReadOnlyError("Betfair response has invalid jsonrpc version")
        if envelope.get("id") != request_id:
            raise BetfairReadOnlyError("Betfair response id does not match request id")
        if "error" in envelope and envelope["error"] is not None:
            error = envelope["error"]
            code = None
            message = None
            if isinstance(error, Mapping):
                code = error.get("code")
                message = error.get("message")
            detail = "Betfair JSON-RPC returned an error"
            if isinstance(code, (str, int)) and not isinstance(code, bool):
                detail += f" code={code}"
            if isinstance(message, str) and message.strip():
                detail += f" message={message.strip()[:160]}"
            raise BetfairReadOnlyError(detail)
        if "result" not in envelope:
            raise BetfairReadOnlyError("Betfair response is missing result")
        return _RpcResult(result=envelope["result"], evidence=evidence)

    def _next_request_id(self) -> int:
        with self._request_lock:
            self._request_id += 1
            return self._request_id

    def _observed_at(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime):
            raise BetfairReadOnlyError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise BetfairReadOnlyError("clock must return timezone-aware datetime")
        return value.isoformat()


def _parse_current_order(
    value: object,
    evidence: BetfairEvidence,
    index: int,
) -> BetfairCurrentOrderObservation:
    raw = _mapping(value, f"currentOrders[{index}]")
    price_size = raw.get("priceSize")
    price = None
    requested_size = None
    if price_size is not None:
        price_map = _mapping(price_size, f"currentOrders[{index}].priceSize")
        price = _number(price_map, "price", "price")
        requested_size = _number(price_map, "size", "size")
    return BetfairCurrentOrderObservation(
        bet_id=_provider_text(raw, "betId", "bet_id"),
        market_id=_provider_text(raw, "marketId", "market_id"),
        selection_id=_provider_int(raw, "selectionId", "selection_id"),
        side=_provider_text(raw, "side", "side"),
        status=_provider_text(raw, "status", "status"),
        placed_date=_provider_text(raw, "placedDate", "placed_date"),
        price=price,
        requested_size=requested_size,
        average_price_matched=_number(
            raw, "averagePriceMatched", "average_price_matched"
        ),
        size_matched=_number(raw, "sizeMatched", "size_matched"),
        size_remaining=_number(raw, "sizeRemaining", "size_remaining"),
        customer_order_ref=_provider_optional_text(
            raw, "customerOrderRef", "customer_order_ref"
        ),
        customer_strategy_ref=_provider_optional_text(
            raw, "customerStrategyRef", "customer_strategy_ref"
        ),
        evidence=evidence,
    )


def _parse_cleared_order(
    value: object,
    evidence: BetfairEvidence,
    index: int,
) -> BetfairClearedOrderObservation:
    raw = _mapping(value, f"clearedOrders[{index}]")
    return BetfairClearedOrderObservation(
        bet_id=_provider_text(raw, "betId", "bet_id"),
        market_id=_provider_text(raw, "marketId", "market_id"),
        selection_id=_provider_int(raw, "selectionId", "selection_id"),
        side=_provider_text(raw, "side", "side"),
        bet_status="SETTLED",
        placed_date=_provider_text(raw, "placedDate", "placed_date"),
        settled_date=_provider_text(raw, "settledDate", "settled_date"),
        price_requested=_number(raw, "priceRequested", "price_requested"),
        price_matched=_number(raw, "priceMatched", "price_matched"),
        size_settled=_number(raw, "sizeSettled", "size_settled"),
        profit=_number(raw, "profit", "profit"),
        customer_order_ref=_provider_optional_text(
            raw, "customerOrderRef", "customer_order_ref"
        ),
        customer_strategy_ref=_provider_optional_text(
            raw, "customerStrategyRef", "customer_strategy_ref"
        ),
        evidence=evidence,
    )


def _unique_bet_ids(orders: Sequence[object], field: str) -> None:
    seen: set[str] = set()
    for order in orders:
        bet_id = getattr(order, "bet_id", None)
        if not isinstance(bet_id, str):
            raise BetfairReadOnlyError(f"{field} order lacks bet_id")
        if bet_id in seen:
            raise BetfairReadOnlyError(f"{field} contains duplicate bet_id {bet_id}")
        seen.add(bet_id)


def _page_bounds(from_record: int, record_count: int) -> None:
    _nonnegative_int(from_record, "from_record")
    _positive_int(record_count, "record_count")
    if record_count > 1000:
        raise BetfairReadOnlyError("record_count cannot exceed Betfair page limit 1000")


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BetfairReadOnlyError(f"{field} must be a JSON object")
    for key in value:
        if not isinstance(key, str):
            raise BetfairReadOnlyError(f"{field} contains a non-string key")
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


def _provider_optional_text(
    value: Mapping[str, object], key: str, field: str
) -> str | None:
    raw = value.get(key)
    if raw is None:
        return None
    return _required_text(raw, field)


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
        raise BetfairReadOnlyError(
            f"{field} must be a JSON number decoded without binary float"
        )
    return _decimal(result, field)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairReadOnlyError(f"{field} must be a non-empty trimmed string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _enum_text(value: object, field: str, allowed: set[str]) -> str:
    text = _required_text(value, field)
    if text not in allowed:
        raise BetfairReadOnlyError(
            f"{field} must be one of {', '.join(sorted(allowed))}"
        )
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
        raise BetfairReadOnlyError(
            f"{field} must be lowercase 64-character SHA-256"
        )
    return text
