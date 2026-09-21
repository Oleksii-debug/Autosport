"""Fail-closed completeness evidence for canonical Betfair read-only pagination.

The existing :class:`BetfairReadOnlyClient` remains the sole provider transport/auth
owner.  These helpers only compose its page APIs and issue immutable evidence that
separates a proven provider-end empty result from partial or unavailable reads.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Callable, Mapping, Sequence
from weakref import ref

from .betfair_account_readonly import (
    BetfairClearedOrderObservation,
    BetfairClearedOrderPage,
    BetfairCurrentOrderObservation,
    BetfairCurrentOrderPage,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
)

_CURRENT_METHOD = "SportsAPING/v1.0/listCurrentOrders"
_CLEARED_METHOD = "SportsAPING/v1.0/listClearedOrders"
_SCHEMA = "autosport.betfair_read_completeness"
_SCHEMA_VERSION = 1


class BetfairReadCompleteness(str, Enum):
    COMPLETE_FOR_DECLARED_QUERY_WINDOW = "COMPLETE_FOR_DECLARED_QUERY_WINDOW"
    PARTIAL = "PARTIAL"
    UNAVAILABLE_TRANSIENT = "UNAVAILABLE_TRANSIENT"
    AUTH_INVALID_OR_EXPIRED = "AUTH_INVALID_OR_EXPIRED"
    INVALID_REQUEST_OR_CONTRACT = "INVALID_REQUEST_OR_CONTRACT"
    HISTORICAL_RETENTION_GAP = "HISTORICAL_RETENTION_GAP"


_DEGRADED = frozenset(
    {
        BetfairReadCompleteness.UNAVAILABLE_TRANSIENT,
        BetfairReadCompleteness.AUTH_INVALID_OR_EXPIRED,
        BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT,
        BetfairReadCompleteness.HISTORICAL_RETENTION_GAP,
    }
)
Page = BetfairCurrentOrderPage | BetfairClearedOrderPage
Order = BetfairCurrentOrderObservation | BetfairClearedOrderObservation


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairPagedReadAcquisition:
    """Evidence for one exact current/cleared-order query attempt."""

    method: str
    query_sha256: str
    attempt_id: str
    started_at: str
    finished_at: str
    completeness: BetfairReadCompleteness
    failure_class: BetfairReadCompleteness | None
    pages: tuple[Page, ...]
    orders: tuple[Order, ...]
    evidence_sha256: str

    def __post_init__(self) -> None:
        self._validate_integrity()

    def _validate_integrity(self) -> None:
        if self.method not in {_CURRENT_METHOD, _CLEARED_METHOD}:
            raise BetfairReadOnlyError("read completeness method is not canonical")
        _sha256_hex(self.query_sha256, "query_sha256")
        _sha256_hex(self.attempt_id, "attempt_id")
        started = _timestamp(self.started_at, "started_at")
        finished = _timestamp(self.finished_at, "finished_at")
        if finished < started:
            raise BetfairReadOnlyError("read completeness finished_at precedes started_at")
        if not isinstance(self.completeness, BetfairReadCompleteness):
            raise BetfairReadOnlyError("read completeness state is not canonical")
        if self.failure_class is not None and self.failure_class not in _DEGRADED:
            raise BetfairReadOnlyError("read completeness failure class is not degraded")
        if not isinstance(self.pages, tuple) or not isinstance(self.orders, tuple):
            raise BetfairReadOnlyError("read completeness evidence must be immutable")

        page_type = BetfairCurrentOrderPage if self.method == _CURRENT_METHOD else BetfairClearedOrderPage
        order_type = (
            BetfairCurrentOrderObservation
            if self.method == _CURRENT_METHOD
            else BetfairClearedOrderObservation
        )
        if any(type(page) is not page_type for page in self.pages):
            raise BetfairReadOnlyError("read completeness page type does not match method")
        if any(type(order) is not order_type for order in self.orders):
            raise BetfairReadOnlyError("read completeness order type does not match method")
        if _dedupe_pages(self.pages) != self.orders:
            raise BetfairReadOnlyError("read completeness orders do not match page evidence")
        _validate_page_chain(self.pages)

        if self.completeness is BetfairReadCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW:
            if self.failure_class is not None or not self.pages or self.pages[-1].more_available:
                raise BetfairReadOnlyError("complete read requires provider-end page evidence")
        elif self.completeness is BetfairReadCompleteness.PARTIAL:
            if not self.pages or self.failure_class is None or not self.pages[-1].more_available:
                raise BetfairReadOnlyError("partial read requires an incomplete retained page prefix")
        else:
            if self.completeness not in _DEGRADED or self.failure_class is not self.completeness:
                raise BetfairReadOnlyError("degraded read must preserve its failure class")
            if self.pages or self.orders:
                raise BetfairReadOnlyError("zero-page degraded read cannot carry provider rows")

        expected = _evidence_digest(
            self.method,
            self.query_sha256,
            self.attempt_id,
            self.started_at,
            self.finished_at,
            self.completeness,
            self.failure_class,
            self.pages,
            self.orders,
        )
        if self.evidence_sha256 != expected:
            raise BetfairReadOnlyError("read completeness evidence digest mismatch")

    def _authority_fingerprint(self) -> str:
        return sha256(repr((
            self.method,
            self.query_sha256,
            self.attempt_id,
            self.started_at,
            self.finished_at,
            self.completeness,
            self.failure_class,
            self.pages,
            self.orders,
            self.evidence_sha256,
        )).encode("utf-8")).hexdigest()

    def assert_authoritative(self) -> None:
        self._validate_integrity()

    def is_authoritative_empty(self) -> bool:
        self.assert_authoritative()
        return (
            self.completeness
            is BetfairReadCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
            and not self.orders
        )

    def require_complete(self) -> tuple[Order, ...]:
        self.assert_authoritative()
        if self.completeness is not BetfairReadCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW:
            raise BetfairReadOnlyError(
                f"Betfair read is not complete for declared query: {self.completeness.value}"
            )
        return self.orders


def classify_betfair_read_failure(error: BetfairReadOnlyError) -> BetfairReadCompleteness:
    """Classify redacted client failures without ever normalizing them to empty data."""
    if not isinstance(error, BetfairReadOnlyError):
        raise TypeError("error must be BetfairReadOnlyError")
    text = str(error).upper()
    if any(marker in text for marker in (
        "INVALID_SESSION_INFORMATION",
        "INVALID_APP_KEY",
        "NO_SESSION",
        "NO_APP_KEY",
        "SUBSCRIPTION_EXPIRED",
        "STATUS 401",
        "STATUS 403",
    )):
        return BetfairReadCompleteness.AUTH_INVALID_OR_EXPIRED
    if any(marker in text for marker in (
        "TOO_MANY_REQUESTS",
        "SERVICE_BUSY",
        "TIMEOUT_ERROR",
        "UNEXPECTED_ERROR",
        "NETWORK REQUEST FAILED",
        "STATUS 408",
        "STATUS 429",
        "STATUS 500",
        "STATUS 502",
        "STATUS 503",
        "STATUS 504",
    )):
        return BetfairReadCompleteness.UNAVAILABLE_TRANSIENT
    return BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT


def acquire_current_orders(
    client: BetfairReadOnlyClient,
    *,
    page_size: int = 1000,
    max_pages: int = 100,
    customer_order_refs: tuple[str, ...] | None = None,
    market_ids: tuple[str, ...] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> BetfairPagedReadAcquisition:
    query = {
        "orderProjection": "ALL",
        "customerOrderRefs": _text_list(customer_order_refs),
        "marketIds": _text_list(market_ids),
        "page_size": _page_size(page_size),
        "max_pages": _positive_int(max_pages, "max_pages"),
    }
    return _acquire(
        client,
        method=_CURRENT_METHOD,
        query=query,
        max_pages=max_pages,
        clock=clock,
        reader=lambda offset: client.read_current_orders_page(
            from_record=offset,
            record_count=page_size,
            customer_order_refs=customer_order_refs,
            market_ids=market_ids,
        ),
    )


def acquire_cleared_orders(
    client: BetfairReadOnlyClient,
    *,
    settled_from: str | None = None,
    bet_status: str = "SETTLED",
    page_size: int = 1000,
    max_pages: int = 100,
    customer_order_refs: tuple[str, ...] | None = None,
    market_ids: tuple[str, ...] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> BetfairPagedReadAcquisition:
    query = {
        "betStatus": _required_text(bet_status, "bet_status"),
        "groupBy": "BET",
        "settledDateRangeFrom": settled_from,
        "customerOrderRefs": _text_list(customer_order_refs),
        "marketIds": _text_list(market_ids),
        "page_size": _page_size(page_size),
        "max_pages": _positive_int(max_pages, "max_pages"),
    }
    return _acquire(
        client,
        method=_CLEARED_METHOD,
        query=query,
        max_pages=max_pages,
        clock=clock,
        reader=lambda offset: client.read_cleared_orders_page(
            from_record=offset,
            record_count=page_size,
            settled_from=settled_from,
            bet_status=bet_status,
            customer_order_refs=customer_order_refs,
            market_ids=market_ids,
        ),
    )


def _acquire(
    client: BetfairReadOnlyClient,
    *,
    method: str,
    query: Mapping[str, object],
    max_pages: int,
    clock: Callable[[], datetime] | None,
    reader: Callable[[int], Page],
) -> BetfairPagedReadAcquisition:
    if type(client) is not BetfairReadOnlyClient:
        raise TypeError("client must be canonical BetfairReadOnlyClient")
    now = clock or (lambda: datetime.now(timezone.utc))
    started_at = _clock_value(now)
    query_sha256 = _digest({
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "method": method,
        "query": dict(query),
    })
    pages: list[Page] = []
    orders: list[Order] = []
    seen: dict[str, Order] = {}
    offset = 0

    for _ in range(max_pages):
        try:
            page = reader(offset)
        except BetfairReadOnlyError as exc:
            failure = classify_betfair_read_failure(exc)
            return _result(
                method, query_sha256, started_at, _clock_value(now),
                BetfairReadCompleteness.PARTIAL if pages else failure,
                failure, tuple(pages), tuple(orders),
            )
        if _append_unique(page, orders, seen):
            return _result(
                method, query_sha256, started_at, _clock_value(now),
                BetfairReadCompleteness.PARTIAL,
                BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT,
                tuple(pages), tuple(orders),
            )
        pages.append(page)
        if not page.more_available:
            return _result(
                method, query_sha256, started_at, _clock_value(now),
                BetfairReadCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
                None, tuple(pages), tuple(orders),
            )
        if not page.orders:
            return _result(
                method, query_sha256, started_at, _clock_value(now),
                BetfairReadCompleteness.PARTIAL,
                BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT,
                tuple(pages), tuple(orders),
            )
        offset += len(page.orders)

    return _result(
        method, query_sha256, started_at, _clock_value(now),
        BetfairReadCompleteness.PARTIAL,
        BetfairReadCompleteness.INVALID_REQUEST_OR_CONTRACT,
        tuple(pages), tuple(orders),
    )


def _result(
    method: str,
    query_sha256: str,
    started_at: str,
    finished_at: str,
    completeness: BetfairReadCompleteness,
    failure_class: BetfairReadCompleteness | None,
    pages: tuple[Page, ...],
    orders: tuple[Order, ...],
) -> BetfairPagedReadAcquisition:
    attempt_id = _digest({
        "schema": f"{_SCHEMA}.attempt",
        "query_sha256": query_sha256,
        "started_at": started_at,
        "finished_at": finished_at,
        "completeness": completeness.value,
        "failure_class": None if failure_class is None else failure_class.value,
        "pages": [_page_evidence(page) for page in pages],
    })
    evidence_sha256 = _evidence_digest(
        method, query_sha256, attempt_id, started_at, finished_at,
        completeness, failure_class, pages, orders,
    )
    return BetfairPagedReadAcquisition(
        method, query_sha256, attempt_id, started_at, finished_at,
        completeness, failure_class, pages, orders, evidence_sha256,
    )


def _evidence_digest(
    method: str,
    query_sha256: str,
    attempt_id: str,
    started_at: str,
    finished_at: str,
    completeness: BetfairReadCompleteness,
    failure_class: BetfairReadCompleteness | None,
    pages: Sequence[Page],
    orders: Sequence[Order],
) -> str:
    return _digest({
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "method": method,
        "query_sha256": query_sha256,
        "attempt_id": attempt_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "completeness": completeness.value,
        "failure_class": None if failure_class is None else failure_class.value,
        "pages": [_page_evidence(page) for page in pages],
        "orders": [
            {"bet_id": order.bet_id, "response_sha256": order.evidence.source_payload_sha256}
            for order in orders
        ],
    })


def _page_evidence(page: Page) -> dict[str, object]:
    return {
        "from_record": page.from_record,
        "record_count": page.record_count,
        "more_available": page.more_available,
        "response_sha256": page.evidence.source_payload_sha256,
    }


def _append_unique(page: Page, orders: list[Order], seen: dict[str, Order]) -> bool:
    pending: list[Order] = []
    for order in page.orders:
        previous = seen.get(order.bet_id)
        if previous is None:
            pending.append(order)
        elif _semantic_row(previous) != _semantic_row(order):
            return True
    for order in pending:
        seen[order.bet_id] = order
        orders.append(order)
    return False


def _dedupe_pages(pages: Sequence[Page]) -> tuple[Order, ...]:
    orders: list[Order] = []
    seen: dict[str, Order] = {}
    for page in pages:
        if _append_unique(page, orders, seen):
            raise BetfairReadOnlyError("read completeness pages contain conflicting bet identity")
    return tuple(orders)


def _semantic_row(order: Order) -> tuple[object, ...]:
    return tuple(getattr(order, item.name) for item in fields(order) if item.name != "evidence")


def _validate_page_chain(pages: Sequence[Page]) -> None:
    expected_offset = 0
    for index, page in enumerate(pages):
        if page.from_record != expected_offset:
            raise BetfairReadOnlyError("read completeness page offsets are not contiguous")
        if index < len(pages) - 1 and not page.more_available:
            raise BetfairReadOnlyError("read completeness contains pages after provider end")
        expected_offset += len(page.orders)


def _digest(value: object) -> str:
    return sha256(json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _clock_value(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BetfairReadOnlyError("read completeness clock must return timezone-aware datetime")
    return value.isoformat()


def _timestamp(value: object, field: str) -> datetime:
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


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairReadOnlyError(f"{field} must be a non-empty trimmed string")
    return value


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BetfairReadOnlyError(f"{field} must be a positive integer")
    return value


def _page_size(value: object) -> int:
    result = _positive_int(value, "page_size")
    if result > 1000:
        raise BetfairReadOnlyError("page_size cannot exceed Betfair page limit 1000")
    return result


def _text_list(value: tuple[str, ...] | None) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, tuple) or not value:
        raise BetfairReadOnlyError("query filter must be a non-empty tuple")
    result = [_required_text(item, "query filter") for item in value]
    if len(result) != len(set(result)):
        raise BetfairReadOnlyError("query filter must not contain duplicates")
    return result


# Reuse the origin-sealing shape already used by BetfairExecutionReadbackEnvelope.
def _install_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}
    raw_current = acquire_current_orders
    raw_cleared = acquire_cleared_orders
    validate = BetfairPagedReadAcquisition.assert_authoritative

    def register(value: BetfairPagedReadAcquisition) -> BetfairPagedReadAcquisition:
        key = id(value)

        def forget(_weakref: object, *, result_id: int = key) -> None:
            issued.pop(result_id, None)

        issued[key] = (ref(value, forget), value._authority_fingerprint())
        return value

    def current(*args: object, **kwargs: object) -> BetfairPagedReadAcquisition:
        return register(raw_current(*args, **kwargs))

    def cleared(*args: object, **kwargs: object) -> BetfairPagedReadAcquisition:
        return register(raw_cleared(*args, **kwargs))

    def assert_authoritative(self: BetfairPagedReadAcquisition) -> None:
        validate(self)
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise BetfairReadOnlyError(
                "read completeness evidence was not issued by canonical acquisition"
            )
        if record[1] != self._authority_fingerprint():
            raise BetfairReadOnlyError("read completeness evidence changed after issuance")

    globals()["acquire_current_orders"] = current
    globals()["acquire_cleared_orders"] = cleared
    BetfairPagedReadAcquisition.assert_authoritative = assert_authoritative  # type: ignore[method-assign]


_install_authority()
del _install_authority
