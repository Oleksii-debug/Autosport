"""Strict read-only Betfair Exchange account JSON-RPC client.

This adapter exposes only authenticated account/details/current-order/cleared-order reads.
Credentials are memory-only; write RPCs are not reachable through the client. Provider-native
numbers remain Decimal observations and every response carries an exact SHA-256 evidence hash.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from threading import Lock
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, urlopen
from weakref import ref

ACCOUNT_JSON_RPC_ENDPOINT = "https://api.betfair.com/exchange/account/json-rpc/v1"
BETTING_JSON_RPC_ENDPOINT = "https://api.betfair.com/exchange/betting/json-rpc/v1"
ADAPTER_ID = "betfair-exchange-jsonrpc-readonly"
ADAPTER_VERSION = "1"
_GET_ACCOUNT_FUNDS = "AccountAPING/v1.0/getAccountFunds"
_GET_ACCOUNT_DETAILS = "AccountAPING/v1.0/getAccountDetails"
_LIST_CURRENT_ORDERS = "SportsAPING/v1.0/listCurrentOrders"
_LIST_CLEARED_ORDERS = "SportsAPING/v1.0/listClearedOrders"
_LIST_MARKET_CATALOGUE = "SportsAPING/v1.0/listMarketCatalogue"
_EXECUTION_CLEARED_STATUSES = ("SETTLED", "VOIDED", "LAPSED", "CANCELLED")
_MAX_PROVIDER_DECIMAL_DIGITS = 512
_MAX_PROVIDER_DECIMAL_ABS_EXPONENT = 4096
_READ_METHOD_ENDPOINT = MappingProxyType({
    _GET_ACCOUNT_FUNDS: ACCOUNT_JSON_RPC_ENDPOINT,
    _GET_ACCOUNT_DETAILS: ACCOUNT_JSON_RPC_ENDPOINT,
    _LIST_CURRENT_ORDERS: BETTING_JSON_RPC_ENDPOINT,
    _LIST_CLEARED_ORDERS: BETTING_JSON_RPC_ENDPOINT,
    _LIST_MARKET_CATALOGUE: BETTING_JSON_RPC_ENDPOINT,
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
        _enum_text(self.status, "status", {"EXECUTABLE", "EXECUTION_COMPLETE"})
        _iso_timestamp(self.placed_date, "placed_date")
        if self.price is not None:
            _positive_decimal(self.price, "price")
        if self.requested_size is not None:
            _nonnegative_decimal(self.requested_size, "requested_size")
        _nonnegative_decimal(self.average_price_matched, "average_price_matched")
        _nonnegative_decimal(self.size_matched, "size_matched")
        _nonnegative_decimal(self.size_remaining, "size_remaining")
        if self.requested_size is not None:
            requested_num, requested_den = self.requested_size.as_integer_ratio()
            matched_num, matched_den = self.size_matched.as_integer_ratio()
            remaining_num, remaining_den = self.size_remaining.as_integer_ratio()
            accounted_num = matched_num * remaining_den + remaining_num * matched_den
            if accounted_num * requested_den > requested_num * matched_den * remaining_den:
                raise BetfairReadOnlyError(
                    "size_matched plus size_remaining cannot exceed requested_size"
                )
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
    event_id: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.bet_id, "bet_id")
        _required_text(self.market_id, "market_id")
        _positive_int(self.selection_id, "selection_id")
        _enum_text(self.side, "side", {"BACK", "LAY"})
        _required_text(self.bet_status, "bet_status")
        placed_at = _iso_timestamp(self.placed_date, "placed_date")
        settled_at = _iso_timestamp(self.settled_date, "settled_date")
        if settled_at < placed_at:
            raise BetfairReadOnlyError("settled_date must not predate placed_date")
        _positive_decimal(self.price_requested, "price_requested")
        _nonnegative_decimal(self.price_matched, "price_matched")
        _nonnegative_decimal(self.size_settled, "size_settled")
        _decimal(self.profit, "profit")
        _optional_text(self.customer_order_ref, "customer_order_ref")
        _optional_text(self.customer_strategy_ref, "customer_strategy_ref")
        _optional_text(self.event_id, "event_id")


@dataclass(frozen=True, slots=True)
class BetfairMarketEventObservation:
    market_id: str
    event_id: str
    evidence: BetfairEvidence
    source: str = "market_catalogue"

    def __post_init__(self) -> None:
        _required_text(self.market_id, "market_id")
        _required_text(self.event_id, "event_id")
        source = _required_text(self.source, "market_event_source")
        if source != "market_catalogue" and not source.startswith("cleared:"):
            raise BetfairReadOnlyError("market event source is not canonical")
        if not isinstance(self.evidence, BetfairEvidence):
            raise BetfairReadOnlyError("market event evidence must be canonical BetfairEvidence")


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


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairExecutionReadbackEnvelope:
    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    action_id: str
    market_id: str
    market_event: BetfairMarketEventObservation
    current_pages: tuple[BetfairCurrentOrderPage, ...]
    cleared_pages_by_status: tuple[tuple[str, tuple[BetfairClearedOrderPage, ...]], ...]
    observed_at: str
    page_size: int
    request_scope_sha256: str
    evidence_sha256: str
    provider_order_ref: str | None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _required_text(self.venue_id, "venue_id")
        _required_text(self.account_id, "account_id")
        _required_text(self.action_id, "action_id")
        if self.provider_order_ref is not None:
            provider_ref = _required_text(
                self.provider_order_ref, "provider_order_ref"
            )
            if len(provider_ref) > 32 or any(
                character not in "0123456789abcdef"
                for character in provider_ref
            ):
                raise BetfairReadOnlyError(
                    "provider_order_ref must be <=32 lowercase hex characters"
                )
        _required_text(self.market_id, "market_id")
        _positive_int(self.page_size, "page_size")
        if self.page_size > 1000:
            raise BetfairReadOnlyError("execution readback page_size exceeds provider limit")
        if self.adapter_id != ADAPTER_ID or self.adapter_version != ADAPTER_VERSION:
            raise BetfairReadOnlyError("execution readback adapter identity mismatch")
        if not isinstance(self.market_event, BetfairMarketEventObservation):
            raise BetfairReadOnlyError("execution readback requires market-event evidence")
        if self.market_event.market_id != self.market_id:
            raise BetfairReadOnlyError("market-event evidence is for a different market")
        if not isinstance(self.current_pages, tuple) or not self.current_pages:
            raise BetfairReadOnlyError("execution readback requires current-order pages")
        if any(not isinstance(page, BetfairCurrentOrderPage) for page in self.current_pages):
            raise BetfairReadOnlyError("execution readback current pages are not canonical")
        if any(page.record_count != self.page_size for page in self.current_pages):
            raise BetfairReadOnlyError("execution readback current page size changed")
        statuses = tuple(item[0] for item in self.cleared_pages_by_status)
        if statuses != _EXECUTION_CLEARED_STATUSES:
            raise BetfairReadOnlyError("execution readback cleared-status coverage is incomplete")
        for status, pages in self.cleared_pages_by_status:
            if status not in _EXECUTION_CLEARED_STATUSES or not pages:
                raise BetfairReadOnlyError("invalid execution cleared-status evidence")
            if any(not isinstance(page, BetfairClearedOrderPage) for page in pages):
                raise BetfairReadOnlyError("execution cleared pages are not canonical")
            if any(page.record_count != self.page_size for page in pages):
                raise BetfairReadOnlyError("execution readback cleared page size changed")
        _iso_timestamp(self.observed_at, "observed_at")
        _sha256_hex(self.request_scope_sha256, "request_scope_sha256")
        _sha256_hex(self.evidence_sha256, "evidence_sha256")

    def assert_authoritative(self) -> None:
        """Validate immutable capture digests; adapter issuance is layered below."""
        expected_scope = _canonical_sha256(
            _execution_request_scope(
                venue_id=self.venue_id,
                account_id=self.account_id,
                action_id=self.action_id,
                provider_order_ref=self.provider_order_ref,
                market_id=self.market_id,
                page_size=self.page_size,
            )
        )
        if self.request_scope_sha256 != expected_scope:
            raise BetfairReadOnlyError(
                "execution readback request scope digest mismatch"
            )
        expected_evidence = _canonical_sha256(
            _execution_evidence_payload(
                request_scope_sha256=self.request_scope_sha256,
                market_event=self.market_event,
                current_pages=self.current_pages,
                cleared_pages_by_status=self.cleared_pages_by_status,
            )
        )
        if self.evidence_sha256 != expected_evidence:
            raise BetfairReadOnlyError(
                "execution readback capture digest mismatch"
            )

    def _authority_fingerprint(self) -> str:
        """Bind every in-memory capture field to the adapter-issued object identity."""
        payload = (
            self.venue_id,
            self.account_id,
            self.adapter_id,
            self.adapter_version,
            self.action_id,
            self.provider_order_ref,
            self.market_id,
            self.market_event,
            self.current_pages,
            self.cleared_pages_by_status,
            self.observed_at,
            self.page_size,
            self.request_scope_sha256,
            self.evidence_sha256,
        )
        return sha256(repr(payload).encode("utf-8")).hexdigest()


def _execution_request_scope(
    *,
    venue_id: str,
    account_id: str,
    action_id: str,
    provider_order_ref: str | None,
    market_id: str,
    page_size: int,
) -> dict[str, object]:
    customer_order_ref = provider_order_ref or action_id
    scope: dict[str, object] = {
        "schema": "autosport.betfair_execution_readback_scope",
        "schema_version": 2 if provider_order_ref is not None else 1,
        "venue_id": venue_id,
        "account_id": account_id,
        "adapter_id": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "action_id": action_id,
        "market_id": market_id,
        "market_catalogue": {
            "method": _LIST_MARKET_CATALOGUE,
            "filter": {"marketIds": [market_id]},
            "marketProjection": ["EVENT"],
            "maxResults": 1,
        },
        "current": {
            "method": _LIST_CURRENT_ORDERS,
            "orderProjection": "ALL",
            "customerOrderRefs": [customer_order_ref],
            "marketIds": [market_id],
            "page_size": page_size,
        },
        "cleared": {
            "method": _LIST_CLEARED_ORDERS,
            "statuses": list(_EXECUTION_CLEARED_STATUSES),
            "groupBy": "BET",
            "customerOrderRefs": [customer_order_ref],
            "marketIds": [market_id],
            "settledDateRange": None,
            "page_size": page_size,
        },
    }
    if provider_order_ref is not None:
        scope["provider_order_ref"] = provider_order_ref
    return scope


def _execution_evidence_payload(
    *,
    request_scope_sha256: str,
    market_event: BetfairMarketEventObservation,
    current_pages: tuple[BetfairCurrentOrderPage, ...],
    cleared_pages_by_status: tuple[
        tuple[str, tuple[BetfairClearedOrderPage, ...]], ...
    ],
) -> dict[str, object]:
    return {
        "request_scope_sha256": request_scope_sha256,
        "market_event": {
            "market_id": market_event.market_id,
            "event_id": market_event.event_id,
            "source": market_event.source,
            "response_sha256": market_event.evidence.source_payload_sha256,
        },
        "current_pages": [
            {
                "from_record": page.from_record,
                "record_count": page.record_count,
                "more_available": page.more_available,
                "response_sha256": page.evidence.source_payload_sha256,
            }
            for page in current_pages
        ],
        "cleared_pages": [
            {
                "status": status,
                "pages": [
                    {
                        "from_record": page.from_record,
                        "record_count": page.record_count,
                        "more_available": page.more_available,
                        "response_sha256": page.evidence.source_payload_sha256,
                    }
                    for page in pages
                ],
            }
            for status, pages in cleared_pages_by_status
        ],
    }


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

    def read_current_orders_page(
        self,
        *,
        from_record: int = 0,
        record_count: int = 1000,
        customer_order_refs: tuple[str, ...] | None = None,
        market_ids: tuple[str, ...] | None = None,
    ) -> BetfairCurrentOrderPage:
        _page_bounds(from_record, record_count)
        params: dict[str, object] = {
            "orderProjection": "ALL",
            "fromRecord": from_record,
            "recordCount": record_count,
        }
        if customer_order_refs is not None:
            params["customerOrderRefs"] = list(
                _canonical_text_tuple(customer_order_refs, "customer_order_refs")
            )
        if market_ids is not None:
            params["marketIds"] = list(_canonical_text_tuple(market_ids, "market_ids"))
        response = self._rpc(_LIST_CURRENT_ORDERS, params)
        report = _mapping(response.result, "listCurrentOrders result")
        raw_orders = _sequence(report.get("currentOrders"), "currentOrders")
        orders = tuple(_parse_current_order(raw, response.evidence, i) for i, raw in enumerate(raw_orders))
        _unique_bet_ids(orders, "currentOrders")
        return BetfairCurrentOrderPage(orders, _provider_bool(report, "moreAvailable"), from_record, record_count, response.evidence)

    def read_cleared_orders_page(
        self,
        *,
        from_record: int = 0,
        record_count: int = 1000,
        settled_from: str | None = None,
        bet_status: str = "SETTLED",
        customer_order_refs: tuple[str, ...] | None = None,
        market_ids: tuple[str, ...] | None = None,
    ) -> BetfairClearedOrderPage:
        _page_bounds(from_record, record_count)
        status = _enum_text(bet_status, "bet_status", set(_EXECUTION_CLEARED_STATUSES))
        params: dict[str, object] = {
            "betStatus": status,
            "groupBy": "BET",
            "fromRecord": from_record,
            "recordCount": record_count,
        }
        if customer_order_refs is not None:
            params["customerOrderRefs"] = list(
                _canonical_text_tuple(customer_order_refs, "customer_order_refs")
            )
        if market_ids is not None:
            params["marketIds"] = list(_canonical_text_tuple(market_ids, "market_ids"))
        if settled_from is not None:
            _iso_timestamp(settled_from, "settled_from")
            params["settledDateRange"] = {"from": settled_from}
        response = self._rpc(_LIST_CLEARED_ORDERS, params)
        report = _mapping(response.result, "listClearedOrders result")
        raw_orders = _sequence(report.get("clearedOrders"), "clearedOrders")
        orders = tuple(
            _parse_cleared_order(raw, response.evidence, i, status)
            for i, raw in enumerate(raw_orders)
        )
        _unique_bet_ids(orders, "clearedOrders")
        return BetfairClearedOrderPage(orders, _provider_bool(report, "moreAvailable"), from_record, record_count, response.evidence)

    def read_market_event(self, market_id: str) -> BetfairMarketEventObservation:
        market = _required_text(market_id, "market_id")
        response = self._rpc(
            _LIST_MARKET_CATALOGUE,
            {
                "filter": {"marketIds": [market]},
                "marketProjection": ["EVENT"],
                "maxResults": 1,
            },
        )
        rows = _sequence(response.result, "listMarketCatalogue result")
        if len(rows) != 1:
            raise BetfairReadOnlyError(
                "exact market-to-event identity is unavailable from listMarketCatalogue"
            )
        row = _mapping(rows[0], "marketCatalogue[0]")
        returned_market = _provider_text(row, "marketId", "market_id")
        if returned_market != market:
            raise BetfairReadOnlyError("marketCatalogue returned a different market")
        event = _mapping(row.get("event"), "marketCatalogue[0].event")
        event_id = _provider_text(event, "id", "event_id")
        return BetfairMarketEventObservation(returned_market, event_id, response.evidence)

    def read_execution_readback(
        self,
        *,
        action_id: str,
        market_id: str,
        provider_order_ref: str | None = None,
        page_size: int = 1000,
        max_pages: int = 100,
    ) -> BetfairExecutionReadbackEnvelope:
        action = _required_text(action_id, "action_id")
        order_ref = action
        if provider_order_ref is not None:
            order_ref = _required_text(provider_order_ref, "provider_order_ref")
            if len(order_ref) > 32 or any(
                character not in "0123456789abcdef"
                for character in order_ref
            ):
                raise BetfairReadOnlyError(
                    "provider_order_ref must be <=32 lowercase hex characters"
                )
        market = _required_text(market_id, "market_id")
        _page_bounds(0, page_size)
        _positive_int(max_pages, "max_pages")
        market_event: BetfairMarketEventObservation | None
        try:
            market_event = self.read_market_event(market)
        except BetfairReadOnlyError as exc:
            if str(exc) != (
                "exact market-to-event identity is unavailable from listMarketCatalogue"
            ):
                raise
            # CLOSED markets are intentionally absent from listMarketCatalogue.
            # A matching BET-level cleared row may still provide provider-native
            # eventId. Empty evidence remains fail-closed.
            market_event = None

        current_pages: list[BetfairCurrentOrderPage] = []
        offset = 0
        for _ in range(max_pages):
            page = self.read_current_orders_page(
                from_record=offset,
                record_count=page_size,
                customer_order_refs=(order_ref,),
                market_ids=(market,),
            )
            current_pages.append(page)
            if not page.more_available:
                break
            if not page.orders:
                raise BetfairReadOnlyError(
                    "execution currentOrders cannot advance from an empty page"
                )
            offset += len(page.orders)
        else:
            raise BetfairReadOnlyError(
                "execution currentOrders pagination exceeded max_pages"
            )

        cleared_groups: list[tuple[str, tuple[BetfairClearedOrderPage, ...]]] = []
        for status in _EXECUTION_CLEARED_STATUSES:
            pages: list[BetfairClearedOrderPage] = []
            offset = 0
            for _ in range(max_pages):
                page = self.read_cleared_orders_page(
                    from_record=offset,
                    record_count=page_size,
                    bet_status=status,
                    customer_order_refs=(order_ref,),
                    market_ids=(market,),
                )
                pages.append(page)
                if not page.more_available:
                    break
                if not page.orders:
                    raise BetfairReadOnlyError(
                        f"execution {status} pagination cannot advance from an empty page"
                    )
                offset += len(page.orders)
            else:
                raise BetfairReadOnlyError(
                    f"execution {status} pagination exceeded max_pages"
                )
            cleared_groups.append((status, tuple(pages)))

        if market_event is None:
            event_sources = [
                (order.event_id, order.evidence, status)
                for status, pages in cleared_groups
                for page in pages
                for order in page.orders
                if order.event_id is not None
            ]
            event_ids = {event_id for event_id, _, _ in event_sources}
            if len(event_ids) != 1 or not event_sources:
                raise BetfairReadOnlyError(
                    "authoritative market-to-event identity is unavailable for execution readback"
                )
            event_id, event_evidence, event_status = event_sources[0]
            assert event_id is not None
            market_event = BetfairMarketEventObservation(
                market,
                event_id,
                event_evidence,
                f"cleared:{event_status}",
            )

        all_evidence = [market_event.evidence]
        all_evidence.extend(page.evidence for page in current_pages)
        for _, pages in cleared_groups:
            all_evidence.extend(page.evidence for page in pages)
        observed_at = max(
            all_evidence,
            key=lambda evidence: _iso_timestamp(evidence.observed_at, "observed_at"),
        ).observed_at
        request_scope = _execution_request_scope(
            venue_id=self._venue_id,
            account_id=self._account_id,
            action_id=action,
            provider_order_ref=provider_order_ref,
            market_id=market,
            page_size=page_size,
        )
        request_scope_sha256 = _canonical_sha256(request_scope)
        current_pages_tuple = tuple(current_pages)
        cleared_groups_tuple = tuple(cleared_groups)
        evidence_sha256 = _canonical_sha256(
            _execution_evidence_payload(
                request_scope_sha256=request_scope_sha256,
                market_event=market_event,
                current_pages=current_pages_tuple,
                cleared_pages_by_status=cleared_groups_tuple,
            )
        )
        return BetfairExecutionReadbackEnvelope(
            self._venue_id,
            self._account_id,
            ADAPTER_ID,
            ADAPTER_VERSION,
            action,
            market,
            market_event,
            current_pages_tuple,
            cleared_groups_tuple,
            observed_at,
            page_size,
            request_scope_sha256,
            evidence_sha256,
            provider_order_ref,
        )

    def _read_all_current_orders_with_evidence(self, *, page_size: int = 1000, max_pages: int = 100) -> tuple[tuple[BetfairCurrentOrderObservation, ...], tuple[BetfairEvidence, ...]]:
        _positive_int(max_pages, "max_pages")
        _page_bounds(0, page_size)
        offset, result, seen, page_evidence = 0, [], set(), []
        for _ in range(max_pages):
            page = self.read_current_orders_page(from_record=offset, record_count=page_size)
            page_evidence.append(page.evidence)
            _extend_unique(result, seen, page.orders, "currentOrders")
            if not page.more_available:
                return tuple(result), tuple(page_evidence)
            if not page.orders:
                raise BetfairReadOnlyError("currentOrders reported moreAvailable with an empty page")
            offset += len(page.orders)
        raise BetfairReadOnlyError("currentOrders pagination exceeded max_pages while more data remained")

    def read_all_current_orders(self, *, page_size: int = 1000, max_pages: int = 100) -> tuple[BetfairCurrentOrderObservation, ...]:
        orders, _ = self._read_all_current_orders_with_evidence(page_size=page_size, max_pages=max_pages)
        return orders

    def _read_all_cleared_orders_with_evidence(self, *, settled_from: str | None = None, page_size: int = 1000, max_pages: int = 100) -> tuple[tuple[BetfairClearedOrderObservation, ...], tuple[BetfairEvidence, ...]]:
        _positive_int(max_pages, "max_pages")
        _page_bounds(0, page_size)
        offset, result, seen, page_evidence = 0, [], set(), []
        for _ in range(max_pages):
            page = self.read_cleared_orders_page(from_record=offset, record_count=page_size, settled_from=settled_from)
            page_evidence.append(page.evidence)
            _extend_unique(result, seen, page.orders, "clearedOrders")
            if not page.more_available:
                return tuple(result), tuple(page_evidence)
            if not page.orders:
                raise BetfairReadOnlyError("clearedOrders reported moreAvailable with an empty page")
            offset += len(page.orders)
        raise BetfairReadOnlyError("clearedOrders pagination exceeded max_pages while more data remained")

    def read_all_cleared_orders(self, *, settled_from: str | None = None, page_size: int = 1000, max_pages: int = 100) -> tuple[BetfairClearedOrderObservation, ...]:
        orders, _ = self._read_all_cleared_orders_with_evidence(settled_from=settled_from, page_size=page_size, max_pages=max_pages)
        return orders

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
        supported = {
            BookmakerCapability.BALANCE_READ,
            BookmakerCapability.OPEN_POSITIONS_READ,
            BookmakerCapability.SETTLED_POSITIONS_READ,
            BookmakerCapability.BET_READBACK,
        }
        if any(c not in supported for c in requested_capabilities):
            raise BetfairReadOnlyError("requested capability is not implemented by the Betfair account adapter")
        details = self.read_account_details()
        funds = self.read_account_funds() if BookmakerCapability.BALANCE_READ in requested_capabilities else None
        if BookmakerCapability.OPEN_POSITIONS_READ in requested_capabilities:
            current, current_page_evidence = self._read_all_current_orders_with_evidence()
        else:
            current, current_page_evidence = (), ()
        if BookmakerCapability.SETTLED_POSITIONS_READ in requested_capabilities:
            cleared, cleared_page_evidence = self._read_all_cleared_orders_with_evidence()
        else:
            cleared, cleared_page_evidence = (), ()
        common_caps = frozenset(requested_capabilities)
        facts = tuple(BookmakerCapabilityFact(c, BookmakerCapabilityState.SUPPORTED) for c in sorted(common_caps, key=lambda x: x.value))
        hashes = [details.evidence.source_payload_sha256]
        if funds is not None:
            hashes.append(funds.evidence.source_payload_sha256)
        hashes.extend(e.source_payload_sha256 for e in current_page_evidence)
        hashes.extend(e.source_payload_sha256 for e in cleared_page_evidence)
        evidence_hash = sha256("|".join(hashes).encode("ascii")).hexdigest()
        profile = BookmakerCapabilityProfile(self._venue_id, self._account_id, ADAPTER_ID, ADAPTER_VERSION, 1, facts, details.evidence.observed_at, f"betfair://account-snapshot/{evidence_hash}", evidence_hash)
        balance = None
        if funds is not None:
            balance = BookmakerBalanceObservation(self._venue_id, self._account_id, ADAPTER_ID, f"funds:{funds.evidence.source_payload_sha256}", details.currency_code, funds.available_to_bet_balance, funds.evidence.observed_at, funds.evidence.source_payload_sha256, total_balance=None, exposure=funds.exposure, retained_commission=funds.retained_commission, exposure_limit=funds.exposure_limit)
        open_positions = tuple(
            self._to_position(o, BookmakerPositionState.OPEN, details.currency_code)
            for o in current
            if o.size_matched > 0
        )
        settled_positions = tuple(self._to_position(o, BookmakerPositionState.SETTLED, details.currency_code) for o in cleared)
        overlap = {p.external_position_id for p in open_positions} & {p.external_position_id for p in settled_positions}
        if overlap:
            raise BetfairReadOnlyError("provider returned positions in both OPEN and SETTLED states")
        return BookmakerAccountSnapshot(profile, common_caps, self._observed_at(), balance, open_positions, settled_positions)

    def _to_position(self, order: object, state: object, currency: str) -> object:
        from .bookmaker_capability import BookmakerPositionObservation
        if isinstance(order, BetfairCurrentOrderObservation):
            amount = order.size_matched
            odds = order.average_price_matched if order.average_price_matched > 0 else None
            semantics = "betfair_size_matched"
        else:
            amount = order.size_settled
            odds = order.price_matched
            semantics = "betfair_size_settled"
        return BookmakerPositionObservation(self._venue_id, self._account_id, ADAPTER_ID, f"{state.value}:{order.bet_id}:{order.evidence.source_payload_sha256}", order.bet_id, state, currency, order.evidence.observed_at, order.evidence.source_payload_sha256, provider_amount=amount, provider_amount_semantics=semantics, provider_side=order.side, decimal_odds=odds)

    def _rpc(self, method: str, params: Mapping[str, object]) -> _RpcResult:
        return self._rpc_with_post(
            method,
            params,
            self._transport.post,
        )

    def _rpc_with_post(
        self,
        method: str,
        params: Mapping[str, object],
        post: Callable[..., bytes],
        *,
        endpoint_map: Mapping[str, str] | None = None,
        observed_at: Callable[[], str] | None = None,
    ) -> _RpcResult:
        endpoints = _READ_METHOD_ENDPOINT if endpoint_map is None else endpoint_map
        endpoint = endpoints.get(method)
        if endpoint is None:
            raise BetfairReadOnlyError("Betfair RPC method is outside the strict read-only allowlist")
        request_id = self._next_request_id()
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": dict(params), "id": request_id}, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": "application/json", "Content-Type": "application/json", "X-Application": self._credentials.application_key, "X-Authentication": self._credentials.session_token}
        payload = post(
            endpoint,
            headers=headers,
            body=body,
            timeout_seconds=self._timeout_seconds,
        )
        if not isinstance(payload, bytes):
            raise BetfairReadOnlyError("Betfair transport must return bytes")
        evidence = BetfairEvidence(
            self._observed_at() if observed_at is None else observed_at(),
            sha256(payload).hexdigest(),
        )
        decoded = _decode_json(payload)
        envelope = _mapping(decoded, "JSON-RPC response")
        if envelope.get("jsonrpc") != "2.0":
            raise BetfairReadOnlyError("Betfair response has invalid jsonrpc version")
        response_id = envelope.get("id")
        if not isinstance(response_id, int) or isinstance(response_id, bool) or response_id != request_id:
            raise BetfairReadOnlyError("Betfair response id does not match request id")
        if "error" in envelope and envelope["error"] is not None:
            if "result" in envelope:
                raise BetfairReadOnlyError("Betfair response contains both error and result")
            error = envelope["error"]
            code = error.get("code") if isinstance(error, Mapping) else None
            message = error.get("message") if isinstance(error, Mapping) else None
            detail = "Betfair JSON-RPC returned an error"
            if code is not None:
                if not isinstance(code, int) or isinstance(code, bool):
                    raise BetfairReadOnlyError("Betfair JSON-RPC returned a malformed error")
                detail += f" code={code}"
            if isinstance(message, str) and message.strip():
                detail += f" message={self._redact_provider_message(message)[:160]}"
            raise BetfairReadOnlyError(detail)
        if "result" not in envelope:
            raise BetfairReadOnlyError("Betfair response is missing result")
        return _RpcResult(envelope["result"], evidence)

    def _redact_provider_message(self, message: str) -> str:
        text = message.strip()
        secrets = {self._credentials.application_key, self._credentials.session_token}
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, "<redacted>")
        return text

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
                raise BetfairReadOnlyError("Betfair JSON contains duplicate object key")
            result[key] = value
        return result
    def reject_constant(value: str) -> object:
        raise BetfairReadOnlyError("Betfair JSON contains non-standard numeric constant")
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


def _parse_cleared_order(
    value: object,
    evidence: BetfairEvidence,
    index: int,
    bet_status: str,
) -> BetfairClearedOrderObservation:
    raw = _mapping(value, f"clearedOrders[{index}]")
    return BetfairClearedOrderObservation(
        _provider_text(raw, "betId", "bet_id"),
        _provider_text(raw, "marketId", "market_id"),
        _provider_int(raw, "selectionId", "selection_id"),
        _provider_text(raw, "side", "side"),
        bet_status,
        _provider_text(raw, "placedDate", "placed_date"),
        _provider_text(raw, "settledDate", "settled_date"),
        _number(raw, "priceRequested", "price_requested"),
        _number(raw, "priceMatched", "price_matched"),
        _number(raw, "sizeSettled", "size_settled"),
        _number(raw, "profit", "profit"),
        _provider_optional_text(raw, "customerOrderRef", "customer_order_ref"),
        _provider_optional_text(raw, "customerStrategyRef", "customer_strategy_ref"),
        evidence,
        _provider_optional_text(raw, "eventId", "event_id"),
    )


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


def _canonical_sha256(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairReadOnlyError("readback evidence is not canonical JSON") from exc
    return sha256(raw).hexdigest()


def _canonical_text_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise BetfairReadOnlyError(f"{field} must be a non-empty tuple")
    result = tuple(_required_text(item, field) for item in value)
    if len(set(result)) != len(result):
        raise BetfairReadOnlyError(f"{field} must not contain duplicates")
    return result


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
    decimal_tuple = value.as_tuple()
    exponent = decimal_tuple.exponent
    if (
        len(decimal_tuple.digits) > _MAX_PROVIDER_DECIMAL_DIGITS
        or not isinstance(exponent, int)
        or isinstance(exponent, bool)
        or abs(exponent) > _MAX_PROVIDER_DECIMAL_ABS_EXPONENT
    ):
        raise BetfairReadOnlyError(f"{field} exceeds provider Decimal resource bounds")
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
            raise BetfairReadOnlyError(f"{field} contains duplicate bet_id")
        seen.add(bet_id)


def _extend_unique(target: list[object], seen: set[str], orders: Sequence[object], field: str) -> None:
    for order in orders:
        bet_id = getattr(order, "bet_id", None)
        if not isinstance(bet_id, str):
            raise BetfairReadOnlyError(f"{field} order lacks canonical bet_id")
        if bet_id in seen:
            raise BetfairReadOnlyError(f"{field} pagination returned duplicate bet_id")
        seen.add(bet_id)
        target.append(order)

# Bind execution-readback authority to captures actually emitted by the canonical
# adapter.  The registration closure is deliberately not exported: importing this
# module exposes neither a seal token nor a registration function that can mint
# authority for caller-constructed DTOs.
def _install_execution_readback_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}
    trusted_clients: dict[int, tuple[object, ...]] = {}
    active_capture_session: ContextVar[list[object] | None] = ContextVar(
        "betfair_authoritative_readback_capture",
        default=None,
    )
    raw_init = BetfairReadOnlyClient.__init__
    raw_read = BetfairReadOnlyClient.read_execution_readback
    raw_rpc = BetfairReadOnlyClient._rpc
    raw_rpc_with_post = BetfairReadOnlyClient._rpc_with_post
    raw_rpc_with_post_code = raw_rpc_with_post.__code__
    validate_integrity = BetfairExecutionReadbackEnvelope.assert_authoritative
    sealed_transport_type = UrllibBetfairHttpTransport
    sealed_transport_post = UrllibBetfairHttpTransport.post
    sealed_transport_post_code = sealed_transport_post.__code__
    sealed_datetime = datetime
    sealed_timezone_utc = timezone.utc
    sealed_read_method_endpoint = MappingProxyType(dict(_READ_METHOD_ENDPOINT))
    sealed_request_type = Request
    sealed_build_opener = build_opener
    sealed_http_error = HTTPError
    sealed_url_error = URLError
    private_opener = sealed_build_opener()
    private_opener_type = type(private_opener)
    private_opener_open = private_opener_type.open
    private_opener_open_code = private_opener_open.__code__
    private_opener_internal_open = private_opener_type._open
    private_opener_internal_open_code = private_opener_internal_open.__code__
    private_opener_call_chain = private_opener_type._call_chain
    private_opener_call_chain_code = private_opener_call_chain.__code__
    private_opener_error = private_opener_type.error
    private_opener_error_code = private_opener_error.__code__
    private_opener_handlers = tuple(private_opener.handlers)

    sealed_error_type = BetfairReadOnlyError
    sealed_evidence_type = BetfairEvidence
    sealed_rpc_result_type = _RpcResult
    sealed_current_order_type = BetfairCurrentOrderObservation
    sealed_cleared_order_type = BetfairClearedOrderObservation
    sealed_current_page_type = BetfairCurrentOrderPage
    sealed_cleared_page_type = BetfairClearedOrderPage
    sealed_market_event_type = BetfairMarketEventObservation
    sealed_envelope_type = BetfairExecutionReadbackEnvelope
    sealed_decimal_type = Decimal
    sealed_json_module = json
    sealed_json_loads = json.loads
    sealed_json_loads_code = getattr(sealed_json_loads, "__code__", None)
    sealed_json_dumps = json.dumps
    sealed_json_dumps_code = getattr(sealed_json_dumps, "__code__", None)
    sealed_json_decode_error = json.JSONDecodeError
    sealed_json_decoder = json.JSONDecoder
    sealed_json_decoder_init = sealed_json_decoder.__init__
    sealed_json_decoder_init_code = getattr(
        sealed_json_decoder_init, "__code__", None
    )
    sealed_json_decoder_decode = sealed_json_decoder.decode
    sealed_json_decoder_decode_code = getattr(
        sealed_json_decoder_decode, "__code__", None
    )
    sealed_json_decoder_raw_decode = sealed_json_decoder.raw_decode
    sealed_json_decoder_raw_decode_code = getattr(
        sealed_json_decoder_raw_decode, "__code__", None
    )
    sealed_json_encoder = json.JSONEncoder
    sealed_json_encoder_init = sealed_json_encoder.__init__
    sealed_json_encoder_init_code = getattr(
        sealed_json_encoder_init, "__code__", None
    )
    sealed_json_encoder_encode = sealed_json_encoder.encode
    sealed_json_encoder_encode_code = getattr(
        sealed_json_encoder_encode, "__code__", None
    )
    sealed_json_encoder_iterencode = sealed_json_encoder.iterencode
    sealed_json_encoder_iterencode_code = getattr(
        sealed_json_encoder_iterencode, "__code__", None
    )
    sealed_json_encoder_default = sealed_json_encoder.default
    sealed_json_encoder_default_code = getattr(
        sealed_json_encoder_default, "__code__", None
    )
    sealed_sha256 = sha256
    sealed_adapter_id = ADAPTER_ID
    sealed_adapter_version = ADAPTER_VERSION
    sealed_list_current = _LIST_CURRENT_ORDERS
    sealed_list_cleared = _LIST_CLEARED_ORDERS
    sealed_list_market = _LIST_MARKET_CATALOGUE
    sealed_cleared_statuses = tuple(_EXECUTION_CLEARED_STATUSES)
    sealed_max_provider_decimal_digits = _MAX_PROVIDER_DECIMAL_DIGITS
    sealed_max_provider_decimal_abs_exponent = (
        _MAX_PROVIDER_DECIMAL_ABS_EXPONENT
    )

    def json_executable_graph_matches() -> bool:
        return bool(
            json is sealed_json_module
            and getattr(sealed_json_module, "loads", None) is sealed_json_loads
            and getattr(sealed_json_loads, "__code__", None)
            is sealed_json_loads_code
            and getattr(sealed_json_module, "dumps", None) is sealed_json_dumps
            and getattr(sealed_json_dumps, "__code__", None)
            is sealed_json_dumps_code
            and getattr(sealed_json_module, "JSONDecodeError", None)
            is sealed_json_decode_error
            and getattr(sealed_json_module, "JSONDecoder", None)
            is sealed_json_decoder
            and getattr(sealed_json_decoder, "__init__", None)
            is sealed_json_decoder_init
            and getattr(sealed_json_decoder_init, "__code__", None)
            is sealed_json_decoder_init_code
            and getattr(sealed_json_decoder, "decode", None)
            is sealed_json_decoder_decode
            and getattr(sealed_json_decoder_decode, "__code__", None)
            is sealed_json_decoder_decode_code
            and getattr(sealed_json_decoder, "raw_decode", None)
            is sealed_json_decoder_raw_decode
            and getattr(sealed_json_decoder_raw_decode, "__code__", None)
            is sealed_json_decoder_raw_decode_code
            and getattr(sealed_json_module, "JSONEncoder", None)
            is sealed_json_encoder
            and getattr(sealed_json_encoder, "__init__", None)
            is sealed_json_encoder_init
            and getattr(sealed_json_encoder_init, "__code__", None)
            is sealed_json_encoder_init_code
            and getattr(sealed_json_encoder, "encode", None)
            is sealed_json_encoder_encode
            and getattr(sealed_json_encoder_encode, "__code__", None)
            is sealed_json_encoder_encode_code
            and getattr(sealed_json_encoder, "iterencode", None)
            is sealed_json_encoder_iterencode
            and getattr(sealed_json_encoder_iterencode, "__code__", None)
            is sealed_json_encoder_iterencode_code
            and getattr(sealed_json_encoder, "default", None)
            is sealed_json_encoder_default
            and getattr(sealed_json_encoder_default, "__code__", None)
            is sealed_json_encoder_default_code
        )

    def capture_opener_dispatch() -> tuple[tuple[str, object, tuple[object, ...]], ...] | None:
        records: list[tuple[str, object, tuple[object, ...]]] = []
        for map_name in ("handle_open", "process_request", "process_response"):
            mapping = getattr(private_opener, map_name, None)
            if type(mapping) is not dict:
                return None
            for key, handlers in mapping.items():
                if type(key) not in (str, int) or type(handlers) is not list:
                    return None
                records.append((map_name, key, tuple(handlers)))

        error_mapping = getattr(private_opener, "handle_error", None)
        if type(error_mapping) is not dict:
            return None
        for protocol, by_code in error_mapping.items():
            if type(protocol) not in (str, int) or type(by_code) is not dict:
                return None
            for code, handlers in by_code.items():
                if type(code) not in (str, int) or type(handlers) is not list:
                    return None
                records.append(
                    (f"handle_error:{protocol}", code, tuple(handlers))
                )
        records.sort(key=lambda item: (item[0], type(item[1]).__name__, str(item[1])))
        return tuple(records)

    private_opener_dispatch = capture_opener_dispatch()
    if private_opener_dispatch is None:
        raise RuntimeError("canonical Betfair private opener graph is invalid")

    def capture_opener_handler_methods() -> tuple[
        tuple[object, str, object, object], ...
    ] | None:
        methods: list[tuple[object, str, object, object]] = []
        seen: set[tuple[int, str]] = set()
        for map_name, key, handlers in private_opener_dispatch:
            if map_name == "handle_open":
                primary_name = f"{key}_open"
            elif map_name == "process_request":
                primary_name = f"{key}_request"
            elif map_name == "process_response":
                primary_name = f"{key}_response"
            elif map_name.startswith("handle_error:"):
                protocol = map_name.split(":", 1)[1]
                primary_name = f"{protocol}_error_{key}"
            else:
                return None
            for handler in handlers:
                for method_name in (
                    primary_name,
                    "do_open",
                    "do_request_",
                    "redirect_request",
                ):
                    identity = (id(handler), method_name)
                    if identity in seen or not hasattr(handler, method_name):
                        continue
                    seen.add(identity)
                    instance_state = getattr(handler, "__dict__", None)
                    if (
                        type(instance_state) is dict
                        and method_name in instance_state
                    ):
                        method = instance_state[method_name]
                    else:
                        method = getattr(type(handler), method_name, None)
                    if method is None:
                        return None
                    methods.append(
                        (
                            handler,
                            method_name,
                            method,
                            getattr(method, "__code__", None),
                        )
                    )
        methods.sort(key=lambda item: (type(item[0]).__name__, item[1], id(item[0])))
        return tuple(methods)

    private_opener_handler_methods = capture_opener_handler_methods()
    if private_opener_handler_methods is None:
        raise RuntimeError("canonical Betfair private opener handlers are invalid")

    def opener_graph_matches() -> bool:
        if (
            type(private_opener) is not private_opener_type
            or getattr(private_opener_type, "open", None) is not private_opener_open
            or getattr(private_opener_open, "__code__", None) is not private_opener_open_code
            or getattr(private_opener_type, "_open", None) is not private_opener_internal_open
            or getattr(private_opener_internal_open, "__code__", None)
            is not private_opener_internal_open_code
            or getattr(private_opener_type, "_call_chain", None)
            is not private_opener_call_chain
            or getattr(private_opener_call_chain, "__code__", None)
            is not private_opener_call_chain_code
            or getattr(private_opener_type, "error", None) is not private_opener_error
            or getattr(private_opener_error, "__code__", None) is not private_opener_error_code
        ):
            return False
        private_state = getattr(private_opener, "__dict__", None)
        if type(private_state) is not dict or any(
            name in private_state for name in ("open", "_open", "_call_chain", "error")
        ):
            return False
        handlers = getattr(private_opener, "handlers", None)
        if type(handlers) is not list or len(handlers) != len(private_opener_handlers):
            return False
        if any(
            current is not expected
            for current, expected in zip(handlers, private_opener_handlers)
        ):
            return False
        current_dispatch = capture_opener_dispatch()
        if current_dispatch is None or len(current_dispatch) != len(private_opener_dispatch):
            return False
        for current, expected in zip(current_dispatch, private_opener_dispatch):
            if current[0] != expected[0] or current[1] != expected[1]:
                return False
            if len(current[2]) != len(expected[2]) or any(
                current_handler is not expected_handler
                for current_handler, expected_handler in zip(current[2], expected[2])
            ):
                return False
        for handler, method_name, expected_method, expected_code in (
            private_opener_handler_methods
        ):
            instance_state = getattr(handler, "__dict__", None)
            if type(instance_state) is not dict:
                return False
            if method_name in instance_state:
                current_method = instance_state[method_name]
            else:
                current_method = getattr(type(handler), method_name, None)
            if (
                current_method is not expected_method
                or getattr(current_method, "__code__", None) is not expected_code
            ):
                return False
        return True

    def trusted_json(payload: bytes) -> object:
        if not json_executable_graph_matches():
            raise sealed_error_type(
                "canonical Betfair JSON executable authority changed"
            )

        def reject_duplicate_pairs(
            pairs: list[tuple[str, object]],
        ) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise sealed_error_type(
                        "Betfair JSON contains duplicate object key"
                    )
                result[key] = value
            return result

        def reject_constant(value: str) -> object:
            raise sealed_error_type(
                "Betfair JSON contains non-standard numeric constant"
            )

        try:
            decoded = sealed_json_loads(
                payload.decode("utf-8"),
                cls=sealed_json_decoder,
                parse_float=sealed_decimal_type,
                object_pairs_hook=reject_duplicate_pairs,
                parse_constant=reject_constant,
            )
        except sealed_error_type:
            raise
        except (UnicodeDecodeError, sealed_json_decode_error):
            raise sealed_error_type(
                "Betfair response is not valid UTF-8 JSON"
            ) from None
        if not json_executable_graph_matches():
            raise sealed_error_type(
                "canonical Betfair JSON executable authority changed"
            )
        return decoded

    def trusted_sha(value: object) -> str:
        if not json_executable_graph_matches():
            raise sealed_error_type(
                "canonical Betfair JSON executable authority changed"
            )
        try:
            payload = sealed_json_dumps(
                value,
                cls=sealed_json_encoder,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise sealed_error_type(
                "trusted readback evidence is not canonical JSON"
            ) from exc
        if not json_executable_graph_matches():
            raise sealed_error_type(
                "canonical Betfair JSON executable authority changed"
            )
        return sealed_sha256(payload).hexdigest()

    def trusted_mapping(value: object, field: str) -> dict[str, object]:
        if type(value) is not dict or any(type(key) is not str for key in value):
            raise sealed_error_type(f"{field} must be a JSON object")
        return value

    def trusted_sequence(value: object, field: str) -> list[object]:
        if type(value) is not list:
            raise sealed_error_type(f"{field} must be a JSON array")
        return value

    def trusted_text(value: object, field: str) -> str:
        if type(value) is not str or not value or value != value.strip():
            raise sealed_error_type(
                f"{field} must be a non-empty trimmed string"
            )
        return value

    def trusted_optional_text(value: object, field: str) -> str | None:
        return None if value is None else trusted_text(value, field)

    def trusted_positive_int(value: object, field: str) -> int:
        if type(value) is not int or value <= 0:
            raise sealed_error_type(f"{field} must be a positive integer")
        return value

    def trusted_number(
        value: dict[str, object],
        key: str,
        field: str,
        *,
        positive: bool = False,
        nonnegative: bool = False,
    ) -> Decimal:
        if key not in value:
            raise sealed_error_type(f"{field} is missing from provider response")
        raw = value[key]
        if type(raw) is sealed_decimal_type:
            result = raw
        elif type(raw) is int:
            result = sealed_decimal_type(raw)
        else:
            raise sealed_error_type(
                f"{field} must be a JSON number decoded without binary float"
            )
        if not result.is_finite():
            raise sealed_error_type(f"{field} must be finite")
        decimal_tuple = result.as_tuple()
        exponent = decimal_tuple.exponent
        if (
            len(decimal_tuple.digits) > sealed_max_provider_decimal_digits
            or type(exponent) is not int
            or abs(exponent) > sealed_max_provider_decimal_abs_exponent
        ):
            raise sealed_error_type(
                f"{field} exceeds provider Decimal resource bounds"
            )
        if positive and result <= 0:
            raise sealed_error_type(f"{field} must be positive")
        if nonnegative and result < 0:
            raise sealed_error_type(f"{field} must be non-negative")
        return result

    def trusted_provider_text(
        value: dict[str, object],
        key: str,
        field: str,
    ) -> str:
        if key not in value:
            raise sealed_error_type(
                f"{field} is missing from provider response"
            )
        return trusted_text(value[key], field)

    def trusted_provider_int(
        value: dict[str, object],
        key: str,
        field: str,
    ) -> int:
        if key not in value:
            raise sealed_error_type(
                f"{field} is missing from provider response"
            )
        return trusted_positive_int(value[key], field)

    def trusted_provider_bool(value: dict[str, object], key: str) -> bool:
        raw = value.get(key)
        if type(raw) is not bool:
            raise sealed_error_type(f"{key} must be bool")
        return raw

    def trusted_iso(value: object, field: str) -> str:
        text = trusted_text(value, field)
        try:
            parsed = sealed_datetime.fromisoformat(text)
        except ValueError:
            raise sealed_error_type(f"{field} must be ISO-8601") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise sealed_error_type(
                f"{field} must include timezone offset"
            )
        return text

    def trusted_evidence_tuple(evidence: object) -> tuple[str, str]:
        if type(evidence) is not sealed_evidence_type:
            raise sealed_error_type(
                "Betfair response evidence is not canonical"
            )
        observed_at = trusted_iso(
            getattr(evidence, "observed_at", None),
            "observed_at",
        )
        digest = getattr(evidence, "source_payload_sha256", None)
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise sealed_error_type(
                "source_payload_sha256 must be lowercase 64-character SHA-256"
            )
        return observed_at, digest

    def trusted_current_order_projection(
        raw_value: object,
        evidence: object,
        index: int,
    ) -> tuple[object, ...]:
        raw = trusted_mapping(raw_value, f"currentOrders[{index}]")
        price_size = raw.get("priceSize")
        price = requested_size = None
        if price_size is not None:
            price_map = trusted_mapping(
                price_size, f"currentOrders[{index}].priceSize"
            )
            price = trusted_number(
                price_map, "price", "price", positive=True
            )
            requested_size = trusted_number(
                price_map, "size", "size", nonnegative=True
            )
        side = trusted_provider_text(raw, "side", "side")
        if side not in {"BACK", "LAY"}:
            raise sealed_error_type("side must be BACK or LAY")
        status = trusted_provider_text(raw, "status", "status")
        if status not in {"EXECUTABLE", "EXECUTION_COMPLETE"}:
            raise sealed_error_type(
                "status must be EXECUTABLE or EXECUTION_COMPLETE"
            )
        placed_date = trusted_iso(
            raw.get("placedDate"), "placed_date"
        )
        average_price_matched = trusted_number(
            raw,
            "averagePriceMatched",
            "average_price_matched",
            nonnegative=True,
        )
        size_matched = trusted_number(
            raw, "sizeMatched", "size_matched", nonnegative=True
        )
        size_remaining = trusted_number(
            raw, "sizeRemaining", "size_remaining", nonnegative=True
        )
        if status == "EXECUTABLE" and size_remaining <= 0:
            raise sealed_error_type(
                "EXECUTABLE current order must retain positive size_remaining"
            )
        if status == "EXECUTION_COMPLETE" and size_remaining != 0:
            raise sealed_error_type(
                "EXECUTION_COMPLETE current order must have zero size_remaining"
            )
        if requested_size is not None:
            requested_num, requested_den = requested_size.as_integer_ratio()
            matched_num, matched_den = size_matched.as_integer_ratio()
            remaining_num, remaining_den = size_remaining.as_integer_ratio()
            accounted_num = (
                matched_num * remaining_den
                + remaining_num * matched_den
            )
            if (
                accounted_num * requested_den
                > requested_num * matched_den * remaining_den
            ):
                raise sealed_error_type(
                    "size_matched plus size_remaining cannot exceed requested_size"
                )
        return (
            trusted_provider_text(raw, "betId", "bet_id"),
            trusted_provider_text(raw, "marketId", "market_id"),
            trusted_provider_int(raw, "selectionId", "selection_id"),
            side,
            status,
            placed_date,
            price,
            requested_size,
            average_price_matched,
            size_matched,
            size_remaining,
            trusted_optional_text(
                raw.get("customerOrderRef"), "customer_order_ref"
            ),
            trusted_optional_text(
                raw.get("customerStrategyRef"), "customer_strategy_ref"
            ),
            trusted_evidence_tuple(evidence),
        )

    def actual_current_order_projection(order: object) -> tuple[object, ...]:
        if type(order) is not sealed_current_order_type:
            raise sealed_error_type(
                "currentOrders contains non-canonical order"
            )
        return (
            order.bet_id,
            order.market_id,
            order.selection_id,
            order.side,
            order.status,
            order.placed_date,
            order.price,
            order.requested_size,
            order.average_price_matched,
            order.size_matched,
            order.size_remaining,
            order.customer_order_ref,
            order.customer_strategy_ref,
            trusted_evidence_tuple(order.evidence),
        )

    def trusted_cleared_order_projection(
        raw_value: object,
        evidence: object,
        index: int,
        status: str,
    ) -> tuple[object, ...]:
        raw = trusted_mapping(raw_value, f"clearedOrders[{index}]")
        side = trusted_provider_text(raw, "side", "side")
        if side not in {"BACK", "LAY"}:
            raise sealed_error_type("side must be BACK or LAY")
        placed_date = trusted_iso(
            raw.get("placedDate"), "placed_date"
        )
        settled_date = trusted_iso(
            raw.get("settledDate"), "settled_date"
        )
        if (
            sealed_datetime.fromisoformat(settled_date)
            < sealed_datetime.fromisoformat(placed_date)
        ):
            raise sealed_error_type(
                "settled_date must not predate placed_date"
            )
        return (
            trusted_provider_text(raw, "betId", "bet_id"),
            trusted_provider_text(raw, "marketId", "market_id"),
            trusted_provider_int(raw, "selectionId", "selection_id"),
            side,
            status,
            placed_date,
            settled_date,
            trusted_number(
                raw, "priceRequested", "price_requested", positive=True
            ),
            trusted_number(
                raw, "priceMatched", "price_matched", nonnegative=True
            ),
            trusted_number(
                raw, "sizeSettled", "size_settled", nonnegative=True
            ),
            trusted_number(raw, "profit", "profit"),
            trusted_optional_text(
                raw.get("customerOrderRef"), "customer_order_ref"
            ),
            trusted_optional_text(
                raw.get("customerStrategyRef"), "customer_strategy_ref"
            ),
            trusted_optional_text(raw.get("eventId"), "event_id"),
            trusted_evidence_tuple(evidence),
        )

    def actual_cleared_order_projection(order: object) -> tuple[object, ...]:
        if type(order) is not sealed_cleared_order_type:
            raise sealed_error_type(
                "clearedOrders contains non-canonical order"
            )
        return (
            order.bet_id,
            order.market_id,
            order.selection_id,
            order.side,
            order.bet_status,
            order.placed_date,
            order.settled_date,
            order.price_requested,
            order.price_matched,
            order.size_settled,
            order.profit,
            order.customer_order_ref,
            order.customer_strategy_ref,
            order.event_id,
            trusted_evidence_tuple(order.evidence),
        )

    def trusted_capture_matches(
        capture: BetfairExecutionReadbackEnvelope,
        witnesses: list[object],
        *,
        venue_id: str,
        account_id: str,
        action_id: str,
        market_id: str,
        provider_order_ref: str | None,
        page_size: int,
    ) -> bool:
        if type(capture) is not sealed_envelope_type:
            return False
        try:
            trusted_text(venue_id, "venue_id")
            trusted_text(account_id, "account_id")
            trusted_text(action_id, "action_id")
            trusted_text(market_id, "market_id")
        except sealed_error_type:
            return False
        if (
            type(page_size) is not int
            or page_size <= 0
            or page_size > 1000
            or capture.venue_id != venue_id
            or capture.account_id != account_id
            or capture.adapter_id != sealed_adapter_id
            or capture.adapter_version != sealed_adapter_version
            or capture.action_id != action_id
            or capture.market_id != market_id
            or capture.provider_order_ref != provider_order_ref
            or capture.page_size != page_size
        ):
            return False
        if provider_order_ref is not None and (
            type(provider_order_ref) is not str
            or not provider_order_ref
            or provider_order_ref != provider_order_ref.strip()
            or len(provider_order_ref) > 32
            or any(
                character not in "0123456789abcdef"
                for character in provider_order_ref
            )
        ):
            return False
        order_ref = provider_order_ref or action_id
        expected_methods = [sealed_list_market]
        expected_methods.extend(
            sealed_list_current for _ in capture.current_pages
        )
        for status, pages in capture.cleared_pages_by_status:
            if status not in sealed_cleared_statuses:
                return False
            expected_methods.extend(
                sealed_list_cleared for _ in pages
            )
        if len(witnesses) != len(expected_methods):
            return False
        if any(
            type(witness) is not tuple or len(witness) != 4
            for witness in witnesses
        ):
            return False
        if tuple(witness[0] for witness in witnesses) != tuple(expected_methods):
            return False

        position = 0
        method, params, result, evidence = witnesses[position]
        position += 1
        if params != {
            "filter": {"marketIds": [market_id]},
            "marketProjection": ["EVENT"],
            "maxResults": 1,
        }:
            return False
        rows = trusted_sequence(result, "listMarketCatalogue result")
        market_projection: tuple[str, str, tuple[str, str]] | None
        if len(rows) == 1:
            row = trusted_mapping(rows[0], "marketCatalogue[0]")
            returned_market = trusted_provider_text(
                row, "marketId", "market_id"
            )
            if returned_market != market_id:
                return False
            event = trusted_mapping(
                row.get("event"), "marketCatalogue[0].event"
            )
            market_projection = (
                returned_market,
                trusted_provider_text(event, "id", "event_id"),
                trusted_evidence_tuple(evidence),
            )
        elif len(rows) == 0:
            market_projection = None
        else:
            return False

        current_offset = 0
        for page in capture.current_pages:
            if type(page) is not sealed_current_page_type:
                return False
            _, params, result, evidence = witnesses[position]
            position += 1
            if params != {
                "orderProjection": "ALL",
                "fromRecord": current_offset,
                "recordCount": page_size,
                "customerOrderRefs": [order_ref],
                "marketIds": [market_id],
            }:
                return False
            report = trusted_mapping(
                result, "listCurrentOrders result"
            )
            raw_orders = trusted_sequence(
                report.get("currentOrders"), "currentOrders"
            )
            expected_orders = tuple(
                trusted_current_order_projection(raw, evidence, index)
                for index, raw in enumerate(raw_orders)
            )
            expected_bet_ids = tuple(order[0] for order in expected_orders)
            if len(set(expected_bet_ids)) != len(expected_bet_ids):
                return False
            actual_orders = tuple(
                actual_current_order_projection(order)
                for order in page.orders
            )
            if expected_orders != actual_orders:
                return False
            more_available = trusted_provider_bool(
                report, "moreAvailable"
            )
            if (
                page.from_record != current_offset
                or page.record_count != page_size
                or page.more_available is not more_available
                or trusted_evidence_tuple(page.evidence)
                != trusted_evidence_tuple(evidence)
            ):
                return False
            if more_available:
                if not raw_orders:
                    return False
                current_offset += len(raw_orders)

        cleared_event_sources: list[
            tuple[str, tuple[str, str], str]
        ] = []
        if tuple(
            status for status, _ in capture.cleared_pages_by_status
        ) != sealed_cleared_statuses:
            return False
        for status, pages in capture.cleared_pages_by_status:
            cleared_offset = 0
            for page in pages:
                if type(page) is not sealed_cleared_page_type:
                    return False
                _, params, result, evidence = witnesses[position]
                position += 1
                if params != {
                    "betStatus": status,
                    "groupBy": "BET",
                    "fromRecord": cleared_offset,
                    "recordCount": page_size,
                    "customerOrderRefs": [order_ref],
                    "marketIds": [market_id],
                }:
                    return False
                report = trusted_mapping(
                    result, "listClearedOrders result"
                )
                raw_orders = trusted_sequence(
                    report.get("clearedOrders"), "clearedOrders"
                )
                expected_orders = tuple(
                    trusted_cleared_order_projection(
                        raw, evidence, index, status
                    )
                    for index, raw in enumerate(raw_orders)
                )
                expected_bet_ids = tuple(
                    order[0] for order in expected_orders
                )
                if len(set(expected_bet_ids)) != len(expected_bet_ids):
                    return False
                actual_orders = tuple(
                    actual_cleared_order_projection(order)
                    for order in page.orders
                )
                if expected_orders != actual_orders:
                    return False
                for projected in expected_orders:
                    event_id = projected[13]
                    if event_id is not None:
                        cleared_event_sources.append(
                            (
                                event_id,
                                projected[14],
                                status,
                            )
                        )
                more_available = trusted_provider_bool(
                    report, "moreAvailable"
                )
                if (
                    page.from_record != cleared_offset
                    or page.record_count != page_size
                    or page.more_available is not more_available
                    or trusted_evidence_tuple(page.evidence)
                    != trusted_evidence_tuple(evidence)
                ):
                    return False
                if more_available:
                    if not raw_orders:
                        return False
                    cleared_offset += len(raw_orders)

        if position != len(witnesses):
            return False
        if type(capture.market_event) is not sealed_market_event_type:
            return False
        if market_projection is not None:
            expected_market_event = (
                market_projection[0],
                market_projection[1],
                "market_catalogue",
                market_projection[2],
            )
        else:
            event_ids = {
                event_id
                for event_id, _, _ in cleared_event_sources
            }
            if len(event_ids) != 1 or not cleared_event_sources:
                return False
            event_id, event_evidence, event_status = (
                cleared_event_sources[0]
            )
            expected_market_event = (
                market_id,
                event_id,
                f"cleared:{event_status}",
                event_evidence,
            )
        actual_market_event = (
            capture.market_event.market_id,
            capture.market_event.event_id,
            capture.market_event.source,
            trusted_evidence_tuple(capture.market_event.evidence),
        )
        if actual_market_event != expected_market_event:
            return False

        evidence_times = [
            trusted_evidence_tuple(witness[3])[0]
            for witness in witnesses
        ]
        expected_observed_at = max(
            evidence_times,
            key=lambda value: sealed_datetime.fromisoformat(value),
        )
        if capture.observed_at != expected_observed_at:
            return False

        request_scope: dict[str, object] = {
            "schema": "autosport.betfair_execution_readback_scope",
            "schema_version": 2 if provider_order_ref is not None else 1,
            "venue_id": venue_id,
            "account_id": account_id,
            "adapter_id": sealed_adapter_id,
            "adapter_version": sealed_adapter_version,
            "action_id": action_id,
            "market_id": market_id,
            "market_catalogue": {
                "method": sealed_list_market,
                "filter": {"marketIds": [market_id]},
                "marketProjection": ["EVENT"],
                "maxResults": 1,
            },
            "current": {
                "method": sealed_list_current,
                "orderProjection": "ALL",
                "customerOrderRefs": [order_ref],
                "marketIds": [market_id],
                "page_size": page_size,
            },
            "cleared": {
                "method": sealed_list_cleared,
                "statuses": list(sealed_cleared_statuses),
                "groupBy": "BET",
                "customerOrderRefs": [order_ref],
                "marketIds": [market_id],
                "settledDateRange": None,
                "page_size": page_size,
            },
        }
        if provider_order_ref is not None:
            request_scope["provider_order_ref"] = provider_order_ref
        expected_scope_sha = trusted_sha(request_scope)
        if capture.request_scope_sha256 != expected_scope_sha:
            return False

        evidence_payload = {
            "request_scope_sha256": expected_scope_sha,
            "market_event": {
                "market_id": capture.market_event.market_id,
                "event_id": capture.market_event.event_id,
                "source": capture.market_event.source,
                "response_sha256": capture.market_event.evidence.source_payload_sha256,
            },
            "current_pages": [
                {
                    "from_record": page.from_record,
                    "record_count": page.record_count,
                    "more_available": page.more_available,
                    "response_sha256": page.evidence.source_payload_sha256,
                }
                for page in capture.current_pages
            ],
            "cleared_pages": [
                {
                    "status": status,
                    "pages": [
                        {
                            "from_record": page.from_record,
                            "record_count": page.record_count,
                            "more_available": page.more_available,
                            "response_sha256": page.evidence.source_payload_sha256,
                        }
                        for page in pages
                    ],
                }
                for status, pages in capture.cleared_pages_by_status
            ],
        }
        return capture.evidence_sha256 == trusted_sha(evidence_payload)

    def trusted_record(
        self: BetfairReadOnlyClient,
    ) -> tuple[object, ...] | None:
        record = trusted_clients.get(id(self))
        if record is None or record[0]() is not self:
            return None
        transport = record[1]
        trusted_clock = record[2]
        trusted_clock_code = record[3]
        if (
            self._transport is not transport
            or type(transport) is not sealed_transport_type
            or self._clock is not trusted_clock
            or getattr(trusted_clock, "__code__", None) is not trusted_clock_code
            or self._credentials is not record[4]
            or self._credentials.application_key != record[5]
            or self._credentials.session_token != record[6]
            or self._venue_id != record[7]
            or self._account_id != record[8]
        ):
            return None
        return record

    def canonical_network_post(
        transport: UrllibBetfairHttpTransport,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        if not opener_graph_matches():
            raise sealed_error_type(
                "canonical Betfair private opener dispatch changed"
            )
        request = sealed_request_type(
            url,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        try:
            with private_opener_open(
                private_opener,
                request,
                timeout=timeout_seconds,
            ) as response:
                payload = response.read(transport._max_response_bytes + 1)
        except sealed_http_error as exc:
            raise sealed_error_type(
                f"Betfair HTTP request failed with status {exc.code}"
            ) from None
        except (sealed_url_error, TimeoutError, OSError):
            raise sealed_error_type("Betfair network request failed") from None
        if not opener_graph_matches():
            raise sealed_error_type(
                "canonical Betfair private opener dispatch changed"
            )
        if len(payload) > transport._max_response_bytes:
            raise sealed_error_type("Betfair response exceeded the size limit")
        return payload

    def authoritative_init(
        self: BetfairReadOnlyClient,
        credentials: BetfairSessionCredentials,
        *,
        transport: BetfairHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        venue_id: str = "betfair",
        account_id: str = "default-account",
    ) -> None:
        caller_supplied_clock = clock is not None

        def product_clock() -> datetime:
            return sealed_datetime.now(sealed_timezone_utc)

        raw_init(
            self,
            credentials,
            transport=transport,
            timeout_seconds=timeout_seconds,
            clock=clock if caller_supplied_clock else product_clock,
            venue_id=venue_id,
            account_id=account_id,
        )
        if (
            type(self) is not BetfairReadOnlyClient
            or transport is not None
            or caller_supplied_clock
        ):
            return
        trusted_transport = self._transport
        if type(trusted_transport) is not sealed_transport_type:
            raise BetfairReadOnlyError(
                "canonical Betfair client did not install the sealed network transport"
            )
        client_id = id(self)

        def forget_client(_weakref: object, *, key: int = client_id) -> None:
            trusted_clients.pop(key, None)

        trusted_clients[client_id] = (
            ref(self, forget_client),
            trusted_transport,
            product_clock,
            product_clock.__code__,
            self._credentials,
            self._credentials.application_key,
            self._credentials.session_token,
            self._venue_id,
            self._account_id,
        )

    def has_trusted_transport(self: BetfairReadOnlyClient) -> bool:
        record = trusted_record(self)
        if record is None:
            return False
        transport = record[1]
        current_post = getattr(sealed_transport_type, "post", None)
        if (
            current_post is not sealed_transport_post
            or getattr(current_post, "__code__", None) is not sealed_transport_post_code
        ):
            return False
        if (
            getattr(BetfairReadOnlyClient, "_rpc", None) is not authoritative_rpc
            or getattr(authoritative_rpc, "__code__", None) is not authoritative_rpc_code
            or raw_rpc_with_post.__code__ is not raw_rpc_with_post_code
            or getattr(private_opener_type, "open", None) is not private_opener_open
            or getattr(private_opener_open, "__code__", None) is not private_opener_open_code
            or not opener_graph_matches()
            or not json_executable_graph_matches()
        ):
            return False
        instance_dict = getattr(transport, "__dict__", {})
        if isinstance(instance_dict, dict) and "post" in instance_dict:
            return False
        return True

    def authoritative_rpc(
        self: BetfairReadOnlyClient,
        method: str,
        params: Mapping[str, object],
    ) -> _RpcResult:
        session = active_capture_session.get()
        tracking = (
            isinstance(session, list)
            and len(session) == 5
            and session[0] is self
        )
        if tracking:
            session[1] = int(session[1]) + 1

        record = trusted_record(self)
        if record is None:
            if tracking:
                session[3] = True
            return raw_rpc(self, method, params)
        transport = record[1]
        if (
            getattr(BetfairReadOnlyClient, "_rpc", None) is not authoritative_rpc
            or raw_rpc_with_post.__code__ is not raw_rpc_with_post_code
            or getattr(private_opener_type, "open", None) is not private_opener_open
            or getattr(private_opener_open, "__code__", None) is not private_opener_open_code
            or not opener_graph_matches()
            or not json_executable_graph_matches()
        ):
            raise sealed_error_type(
                "canonical Betfair network authority changed"
            )

        exchange: list[tuple[bytes, bytes]] = []

        def post(
            url: str,
            *,
            headers: Mapping[str, str],
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            payload = canonical_network_post(
                transport,
                url,
                headers=headers,
                body=body,
                timeout_seconds=timeout_seconds,
            )
            exchange.append((body, payload))
            return payload

        result = raw_rpc_with_post(
            self,
            method,
            params,
            post,
            endpoint_map=sealed_read_method_endpoint,
            observed_at=lambda: sealed_datetime.now(
                sealed_timezone_utc
            ).isoformat(),
        )
        if len(exchange) != 1:
            raise sealed_error_type(
                "canonical Betfair RPC did not produce one network exchange"
            )
        request_body, response_body = exchange[0]
        request_document = trusted_mapping(
            trusted_json(request_body),
            "JSON-RPC request",
        )
        if (
            request_document.get("jsonrpc") != "2.0"
            or request_document.get("method") != method
            or request_document.get("params") != dict(params)
        ):
            raise sealed_error_type(
                "canonical Betfair RPC request bytes changed"
            )
        request_id = request_document.get("id")
        if type(request_id) is not int:
            raise sealed_error_type(
                "canonical Betfair RPC request id is invalid"
            )

        response_document = trusted_mapping(
            trusted_json(response_body),
            "JSON-RPC response",
        )
        if response_document.get("jsonrpc") != "2.0":
            raise sealed_error_type(
                "Betfair response has invalid jsonrpc version"
            )
        if response_document.get("id") != request_id:
            raise sealed_error_type(
                "Betfair response id does not match request id"
            )
        if (
            "error" in response_document
            and response_document["error"] is not None
        ):
            raise sealed_error_type(
                "Betfair JSON-RPC returned an error"
            )
        if "result" not in response_document:
            raise sealed_error_type(
                "Betfair response is missing result"
            )
        if type(result) is not sealed_rpc_result_type:
            raise sealed_error_type(
                "canonical Betfair RPC result type changed"
            )
        evidence_tuple = trusted_evidence_tuple(result.evidence)
        if evidence_tuple[1] != sealed_sha256(response_body).hexdigest():
            raise sealed_error_type(
                "Betfair response evidence is not bound to network bytes"
            )
        trusted_result = sealed_rpc_result_type(
            response_document["result"],
            result.evidence,
        )
        if (
            type(trusted_result) is not sealed_rpc_result_type
            or trusted_result.result is not response_document["result"]
            or trusted_result.evidence is not result.evidence
        ):
            raise sealed_error_type(
                "canonical Betfair RPC result construction changed"
            )
        if tracking:
            session[2] = int(session[2]) + 1
            session[4].append(
                (
                    method,
                    request_document["params"],
                    trusted_result.result,
                    trusted_result.evidence,
                )
            )
        return trusted_result

    authoritative_rpc_code = authoritative_rpc.__code__

    def authoritative_read(
        self: BetfairReadOnlyClient,
        *,
        action_id: str,
        market_id: str,
        provider_order_ref: str | None = None,
        page_size: int = 1000,
        max_pages: int = 100,
    ) -> BetfairExecutionReadbackEnvelope:
        trusted_at_start = has_trusted_transport(self)
        session: list[object] = [self, 0, 0, False, []]
        context_token = active_capture_session.set(session)
        try:
            capture = raw_read(
                self,
                action_id=action_id,
                market_id=market_id,
                provider_order_ref=provider_order_ref,
                page_size=page_size,
                max_pages=max_pages,
            )
        finally:
            active_capture_session.reset(context_token)

        expected_rpc_count = (
            1
            + len(capture.current_pages)
            + sum(
                len(pages)
                for _, pages in capture.cleared_pages_by_status
            )
        )
        capture_id = id(capture)
        def forget(_weakref: object, *, key: int = capture_id) -> None:
            issued.pop(key, None)

        if (
            trusted_at_start
            and session[1] == expected_rpc_count
            and session[2] == expected_rpc_count
            and session[3] is False
            and type(session[4]) is list
            and trusted_capture_matches(
                capture,
                session[4],
                venue_id=self._venue_id,
                account_id=self._account_id,
                action_id=action_id,
                market_id=market_id,
                provider_order_ref=provider_order_ref,
                page_size=page_size,
            )
            and has_trusted_transport(self)
        ):
            issued[capture_id] = (
                ref(capture, forget),
                capture._authority_fingerprint(),
            )
        return capture

    def assert_authoritative(self: BetfairExecutionReadbackEnvelope) -> None:
        # Preserve the canonical scope/evidence checks first so any ordinary
        # tamper is rejected for its exact invariant before origin is considered.
        validate_integrity(self)
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise BetfairReadOnlyError(
                "execution readback was not issued by canonical BetfairReadOnlyClient"
            )
        if record[1] != self._authority_fingerprint():
            raise BetfairReadOnlyError(
                "execution readback changed after canonical adapter capture"
            )

    BetfairReadOnlyClient.__init__ = authoritative_init
    BetfairReadOnlyClient._rpc = authoritative_rpc
    BetfairReadOnlyClient.read_execution_readback = authoritative_read
    BetfairExecutionReadbackEnvelope.assert_authoritative = assert_authoritative


_install_execution_readback_authority()
del _install_execution_readback_authority
