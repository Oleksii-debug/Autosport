"""Strict read-only ProphetX sandbox wallet transaction evidence.

Reads GET /partner/v4/mm/get_transactions only. Rows remain provider evidence and never grant
execution, settlement, P&L, bankroll, or retry authority.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import math
from typing import Callable
from urllib.parse import urlencode
from weakref import WeakKeyDictionary

from .prophetx_account_readonly import (
    ADAPTER_ID,
    PROVIDER_CURRENCY,
    ProphetXHttpResponse,
    ProphetXReadOnlyError,
    ProphetXSessionToken,
    _decode_json,
    _iso_timestamp,
    _mapping,
    _required_text,
)
from .prophetx_transactions_transport import (
    MAX_RESPONSE_BYTES,
    TRANSACTIONS_URL,
    ProphetXTransactionsTransport,
    UrllibProphetXTransactionsTransport,
)

_MAX_PAGES = 100
_MAX_MONEY_DIGITS = 256
_MAX_MONEY_ABS_EXPONENT = 128
STATUSES = frozenset({"Completed", "Pending", "Processing", "Failed", "Expired", "Invalid"})
TYPES = frozenset({
    "TRADE", "DEPOSIT", "PAY", "WITHDRAW", "REJECT_WITHDRAW", "APPROVE_WITHDRAW",
    "REFUND", "CANCEL", "ADJUSTMENT_REDUCE", "COMMISSION", "ADJUSTMENT_INCREASE",
    "VOID", "PUSH",
})


def _filter_text(value: object, field: str) -> str:
    text = _required_text(value, field)
    if len(text) > 4096 or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ProphetXReadOnlyError(f"{field} is not a safe request value")
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ProphetXReadOnlyError(f"{field} must be valid UTF-8 text") from None
    return text


def _provider_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 4096:
        raise ProphetXReadOnlyError(f"{field} must be a bounded string or null")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ProphetXReadOnlyError(f"{field} contains control characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ProphetXReadOnlyError(f"{field} must be valid UTF-8 text") from None
    return value


def _money(value: object, field: str, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ProphetXReadOnlyError(f"{field} must be an exact finite JSON number")
    _sign, digits, exponent = value.as_tuple()
    if (
        len(digits) > _MAX_MONEY_DIGITS
        or abs(int(exponent)) > _MAX_MONEY_ABS_EXPONENT
    ):
        raise ProphetXReadOnlyError(
            f"{field} exceeds bounded exact-money representation"
        )
    if nonnegative and (value < 0 or value.as_tuple().sign):
        raise ProphetXReadOnlyError(
            f"{field} must use an unsigned non-negative representation"
        )
    return value


def _exact_add(left: Decimal, right: Decimal) -> Decimal:
    """Add finite Decimals without depending on the process Decimal context."""

    def parts(value: Decimal) -> tuple[int, int]:
        sign, digits, exponent = value.as_tuple()
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        if sign:
            coefficient = -coefficient
        return coefficient, int(exponent)

    left_coefficient, left_exponent = parts(left)
    right_coefficient, right_exponent = parts(right)
    exponent = min(left_exponent, right_exponent)
    total = (
        left_coefficient * (10 ** (left_exponent - exponent))
        + right_coefficient * (10 ** (right_exponent - exponent))
    )
    if total == 0:
        return Decimal((0, (0,), exponent))
    sign = 1 if total < 0 else 0
    digits = tuple(int(character) for character in str(abs(total)))
    return Decimal((sign, digits, exponent))


@dataclass(frozen=True, slots=True)
class ProphetXTransactionQuery:
    limit: int = 20
    next_cursor: str | None = None
    from_unix: int | None = None
    to_unix: int | None = None
    market_id: str | None = None
    trade_id: str | None = None
    event_id: str | None = None
    transaction_type: str | None = None

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= 1000:
            raise ProphetXReadOnlyError("limit must be an integer between 1 and 1000")
        if self.next_cursor is not None:
            _filter_text(self.next_cursor, "next_cursor")
        for field in ("from_unix", "to_unix"):
            value = getattr(self, field)
            if value is not None and type(value) is not int:
                raise ProphetXReadOnlyError(f"{field} must be an integer Unix timestamp")
        if self.from_unix is not None and self.to_unix is not None and self.from_unix > self.to_unix:
            raise ProphetXReadOnlyError("from_unix must not exceed to_unix")
        for field in ("market_id", "trade_id", "event_id"):
            value = getattr(self, field)
            if value is not None:
                _filter_text(value, field)
        if self.transaction_type is not None:
            value = _filter_text(self.transaction_type, "transaction_type")
            if value not in TYPES:
                raise ProphetXReadOnlyError("unknown documented ProphetX transaction_type")

    @property
    def request_url(self) -> str:
        items: list[tuple[str, str]] = [("limit", str(self.limit))]
        for key, value in (
            ("next_cursor", self.next_cursor), ("from", self.from_unix), ("to", self.to_unix),
            ("market_id", self.market_id), ("trade_id", self.trade_id), ("event_id", self.event_id),
            ("transaction_type", self.transaction_type),
        ):
            if value is not None:
                items.append((key, str(value)))
        return f"{TRANSACTIONS_URL}?{urlencode(items)}"

    def with_cursor(self, cursor: str) -> "ProphetXTransactionQuery":
        return replace(self, next_cursor=_filter_text(cursor, "next_cursor"))


@dataclass(frozen=True, slots=True)
class ProphetXWalletTransaction:
    status: str
    user_id: str
    transaction_type: str
    transaction_sub_type: str | None
    amount: Decimal
    change: Decimal
    balance: Decimal
    balance_before: Decimal
    details: str | None
    market_id: str | None
    event_id: str | None
    trade_id: str | None
    description: str | None
    created_at: str
    currency: str = PROVIDER_CURRENCY


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ProphetXTransactionPage:
    query: ProphetXTransactionQuery
    transactions: tuple[ProphetXWalletTransaction, ...]
    next_cursor: str | None
    observed_at: str
    source_payload_sha256: str


_PROVIDER_TRANSPORTS: WeakKeyDictionary[object, UrllibProphetXTransactionsTransport] = (
    WeakKeyDictionary()
)
_ISSUED_PAGES: WeakKeyDictionary[
    ProphetXTransactionPage, tuple[object, str]
] = WeakKeyDictionary()


class ProphetXTransactionsClient:
    """Bounded acquisition with process-local proof of exact canonical provider origin."""

    def __init__(
        self,
        session: ProphetXSessionToken,
        *,
        transport: ProphetXTransactionsTransport | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(session, ProphetXSessionToken):
            raise TypeError("session must be ProphetXSessionToken")
        if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be positive and finite")
        self._session = session
        if transport is None:
            canonical = UrllibProphetXTransactionsTransport()
            self._transport: ProphetXTransactionsTransport = canonical
            if type(self) is ProphetXTransactionsClient:
                _PROVIDER_TRANSPORTS[self] = canonical
        else:
            self._transport = transport
        self._timeout = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(adapter_id={ADAPTER_ID!r}, environment='sandbox')"

    def read_page(self, query: ProphetXTransactionQuery | None = None) -> ProphetXTransactionPage:
        query = query or ProphetXTransactionQuery()
        if not isinstance(query, ProphetXTransactionQuery):
            raise TypeError("query must be ProphetXTransactionQuery")
        url = query.request_url
        response = self._transport.get(
            url,
            headers={"Accept": "application/json", "Accept-Encoding": "identity",
                     "Authorization": f"Bearer {self._session.access_token}"},
            timeout_seconds=self._timeout,
        )
        self._validate_response(response, url)
        decoded = _decode_json(response.body)
        data = _mapping(_mapping(decoded, "transaction response").get("data"), "transaction response.data")
        raw = data.get("transactions")
        if not isinstance(raw, list) or len(raw) > query.limit:
            raise ProphetXReadOnlyError("transactions must be an array within requested limit")
        transactions = tuple(_parse_row(item, i) for i, item in enumerate(raw))
        cursor_raw = data.get("next_cursor")
        cursor = None if cursor_raw is None else _filter_text(cursor_raw, "next_cursor")
        observed_at = self._observed_at()
        page = ProphetXTransactionPage(
            query, transactions, cursor, observed_at, sha256(response.body).hexdigest()
        )
        canonical = _PROVIDER_TRANSPORTS.get(self)
        if (
            type(self) is ProphetXTransactionsClient
            and canonical is not None
            and type(canonical) is UrllibProphetXTransactionsTransport
            and self._transport is canonical
        ):
            _ISSUED_PAGES[page] = (self, _fingerprint(page))
        return page

    def read_all(
        self, query: ProphetXTransactionQuery | None = None, *, max_pages: int = _MAX_PAGES
    ) -> tuple[ProphetXTransactionPage, ...]:
        if type(max_pages) is not int or not 1 <= max_pages <= _MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {_MAX_PAGES}")
        current = query or ProphetXTransactionQuery()
        if not isinstance(current, ProphetXTransactionQuery):
            raise TypeError("query must be ProphetXTransactionQuery")
        pages: list[ProphetXTransactionPage] = []
        seen = {current.next_cursor} if current.next_cursor else set()
        for _ in range(max_pages):
            page = self.read_page(current)
            pages.append(page)
            if page.next_cursor is None:
                return tuple(pages)
            if page.next_cursor in seen:
                raise ProphetXReadOnlyError("ProphetX pagination repeated a cursor")
            seen.add(page.next_cursor)
            current = current.with_cursor(page.next_cursor)
        raise ProphetXReadOnlyError("ProphetX pagination exceeded configured page bound")

    def provider_origin_proven(self, page: ProphetXTransactionPage) -> bool:
        """Compatibility convenience; authority lives in the exact module verifier."""
        return prophetx_transaction_provider_origin_proven(self, page)

    @staticmethod
    def _validate_response(response: ProphetXHttpResponse, url: str) -> None:
        if not isinstance(response, ProphetXHttpResponse) or response.status != 200:
            raise ProphetXReadOnlyError("ProphetX transaction response did not return HTTP 200")
        if response.final_url != url:
            raise ProphetXReadOnlyError("ProphetX transaction response URL changed")
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise ProphetXReadOnlyError("ProphetX transaction response exceeded size limit")
        if response.content_encoding and response.content_encoding.strip().lower() != "identity":
            raise ProphetXReadOnlyError("unsupported transaction content encoding")
        if response.content_type is None or response.content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ProphetXReadOnlyError("transaction Content-Type must be application/json")

    def _observed_at(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ProphetXReadOnlyError("clock must return timezone-aware datetime")
        return value.isoformat()


def _parse_row(value: object, index: int) -> ProphetXWalletTransaction:
    row = _mapping(value, f"transactions[{index}]")
    status = _required_text(row.get("status"), "status")
    tx_type = _required_text(row.get("transaction_type"), "transaction_type")
    if status not in STATUSES or tx_type not in TYPES:
        raise ProphetXReadOnlyError("transaction status/type is outside documented ProphetX vocabulary")
    created_at = _filter_text(row.get("created_at"), "created_at")
    _iso_timestamp(created_at, "created_at")
    amount = _money(row.get("amount"), "amount", nonnegative=True)
    change = _money(row.get("change"), "change")
    balance = _money(row.get("balance"), "balance")
    balance_before = _money(row.get("balance_before"), "balance_before")
    if _exact_add(balance_before, change) != balance:
        raise ProphetXReadOnlyError(
            "transaction balance must equal balance_before plus change"
        )
    return ProphetXWalletTransaction(
        status=status,
        user_id=_filter_text(row.get("user_id"), "user_id"),
        transaction_type=tx_type,
        transaction_sub_type=_provider_text(row.get("transaction_sub_type"), "transaction_sub_type"),
        amount=amount,
        change=change,
        balance=balance,
        balance_before=balance_before,
        details=_provider_text(row.get("details"), "details"),
        market_id=_provider_text(row.get("market_id"), "market_id"),
        event_id=_provider_text(row.get("event_id"), "event_id"),
        trade_id=_provider_text(row.get("trade_id"), "trade_id"),
        description=_provider_text(row.get("description"), "description"),
        created_at=created_at,
    )


def prophetx_transaction_provider_origin_proven(
    client: object, page: object
) -> bool:
    """Verify exact process-local provider origin without caller-owned authority state."""
    if (
        type(client) is not ProphetXTransactionsClient
        or type(page) is not ProphetXTransactionPage
    ):
        return False
    canonical = _PROVIDER_TRANSPORTS.get(client)
    if (
        canonical is None
        or type(canonical) is not UrllibProphetXTransactionsTransport
        or client._transport is not canonical
    ):
        return False
    issued = _ISSUED_PAGES.get(page)
    if issued is None or issued[0] is not client:
        return False
    try:
        return issued[1] == _fingerprint(page)
    except (ProphetXReadOnlyError, TypeError, ValueError, UnicodeEncodeError):
        return False


def _fingerprint(page: ProphetXTransactionPage) -> str:
    payload = {
        "next_cursor": page.next_cursor,
        "observed_at": page.observed_at,
        "query_url": page.query.request_url,
        "source_payload_sha256": page.source_payload_sha256,
        "transactions": [
            {
                "amount": str(tx.amount),
                "balance": str(tx.balance),
                "balance_before": str(tx.balance_before),
                "change": str(tx.change),
                "created_at": tx.created_at,
                "currency": tx.currency,
                "description": tx.description,
                "details": tx.details,
                "event_id": tx.event_id,
                "market_id": tx.market_id,
                "status": tx.status,
                "trade_id": tx.trade_id,
                "transaction_sub_type": tx.transaction_sub_type,
                "transaction_type": tx.transaction_type,
                "user_id": tx.user_id,
            }
            for tx in page.transactions
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
