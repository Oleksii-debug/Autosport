"""Fail-closed completeness authority for Betfair read-only acquisition.

The transport in :mod:`autosport.betfair_account_readonly` intentionally raises on
provider/transport failures.  This module projects those outcomes into immutable,
query-bound acquisition evidence so downstream code can distinguish a genuinely
complete empty result from an unavailable or partial observation.

No provider write operation is exposed here.  Completeness is issued only by this
observer around the existing canonical ``BetfairReadOnlyClient``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from threading import Lock
from typing import Callable
import weakref

from . import _campaign_provider_scope_devapp_identity as _client_origin
from .betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairAccountDetailsObservation,
    BetfairAccountFundsObservation,
    BetfairClearedOrderObservation,
    BetfairCurrentOrderObservation,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    UrllibBetfairHttpTransport,
)


# Freeze the canonical provider read dispatch used by completeness authority.
# Exact client type alone is insufficient: ordinary Python instances can shadow
# methods in __dict__, and class methods can be rebound or have __code__ replaced.
_CANONICAL_READ_ACCOUNT_FUNDS = BetfairReadOnlyClient.read_account_funds
_CANONICAL_READ_ACCOUNT_DETAILS = BetfairReadOnlyClient.read_account_details
_CANONICAL_READ_CURRENT_ORDERS_PAGE = BetfairReadOnlyClient.read_current_orders_page
_CANONICAL_READ_CLEARED_ORDERS_PAGE = BetfairReadOnlyClient.read_cleared_orders_page
_CANONICAL_RPC = BetfairReadOnlyClient._rpc
_CANONICAL_READ_DISPATCH = (
    ("read_account_funds", _CANONICAL_READ_ACCOUNT_FUNDS, _CANONICAL_READ_ACCOUNT_FUNDS.__code__),
    ("read_account_details", _CANONICAL_READ_ACCOUNT_DETAILS, _CANONICAL_READ_ACCOUNT_DETAILS.__code__),
    ("read_current_orders_page", _CANONICAL_READ_CURRENT_ORDERS_PAGE, _CANONICAL_READ_CURRENT_ORDERS_PAGE.__code__),
    ("read_cleared_orders_page", _CANONICAL_READ_CLEARED_ORDERS_PAGE, _CANONICAL_READ_CLEARED_ORDERS_PAGE.__code__),
    ("_rpc", _CANONICAL_RPC, _CANONICAL_RPC.__code__),
)


class BetfairObservationCompleteness(str, Enum):
    """What the acquisition attempt can authoritatively say about its query."""

    COMPLETE_FOR_DECLARED_QUERY_WINDOW = "complete_for_declared_query_window"
    PARTIAL = "partial"
    UNAVAILABLE_TRANSIENT = "unavailable_transient"
    AUTH_INVALID_OR_EXPIRED = "auth_invalid_or_expired"
    INVALID_REQUEST_OR_CONTRACT = "invalid_request_or_contract"
    HISTORICAL_RETENTION_GAP = "historical_retention_gap"


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairReadCompletenessWitness:
    operation: str
    completeness: BetfairObservationCompleteness
    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    query_sha256: str
    attempt_id: str
    started_at: str
    finished_at: str
    pages: tuple[tuple[int, int, bool, str], ...]
    rows_observed: int
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _text(self.operation, "operation")
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        if self.adapter_id != ADAPTER_ID or self.adapter_version != ADAPTER_VERSION:
            raise BetfairReadOnlyError("completeness witness adapter identity mismatch")
        _sha(self.query_sha256, "query_sha256")
        _sha(self.attempt_id, "attempt_id")
        started = _time(self.started_at, "started_at").astimezone(timezone.utc)
        finished = _time(self.finished_at, "finished_at").astimezone(timezone.utc)
        if finished < started:
            raise BetfairReadOnlyError("finished_at must not precede started_at")
        if not isinstance(self.completeness, BetfairObservationCompleteness):
            raise BetfairReadOnlyError("completeness must be canonical")
        if not isinstance(self.pages, tuple):
            raise BetfairReadOnlyError("pages must be a tuple")
        for page in self.pages:
            if (
                not isinstance(page, tuple)
                or len(page) != 4
                or not isinstance(page[0], int)
                or isinstance(page[0], bool)
                or page[0] < 0
                or not isinstance(page[1], int)
                or isinstance(page[1], bool)
                or page[1] <= 0
                or not isinstance(page[2], bool)
            ):
                raise BetfairReadOnlyError("page coverage is malformed")
            _sha(page[3], "page response digest")
        if (
            not isinstance(self.rows_observed, int)
            or isinstance(self.rows_observed, bool)
            or self.rows_observed < 0
        ):
            raise BetfairReadOnlyError("rows_observed must be a non-negative integer")
        if self.failure_code is not None:
            _text(self.failure_code, "failure_code")
        if self.completeness is BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW:
            if self.failure_code is not None:
                raise BetfairReadOnlyError("complete observation cannot carry a failure_code")
            if not self.pages and self.operation in {"listCurrentOrders", "listClearedOrders"}:
                raise BetfairReadOnlyError("complete paged observation requires provider-end evidence")
        elif self.failure_code is None:
            raise BetfairReadOnlyError("incomplete observation requires a failure_code")

    @property
    def authoritative(self) -> bool:
        try:
            self.assert_authoritative()
        except BetfairReadOnlyError:
            return False
        return True

    def assert_issued(self) -> None:
        """Fail closed until closure-private issuance verification is installed."""
        raise BetfairReadOnlyError(
            "Betfair completeness issuance authority is not installed"
        )

    def assert_authoritative(self) -> None:
        """Fail closed until closure-private issuance verification is installed."""
        if (
            self.completeness
            is not BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
        ):
            raise BetfairReadOnlyError(
                f"Betfair read is not complete: {self.completeness.value}"
            )
        self.assert_issued()

    def assert_authoritative_for(self, *, venue_id: str, account_id: str) -> None:
        """Require positive completeness for the exact configured client scope."""
        self.assert_authoritative()
        if self.venue_id != _text(venue_id, "expected venue_id"):
            raise BetfairReadOnlyError("Betfair completeness venue scope mismatch")
        if self.account_id != _text(account_id, "expected account_id"):
            raise BetfairReadOnlyError("Betfair completeness account scope mismatch")

    def _fingerprint(self) -> str:
        return sha256(
            repr(
                (
                    self.operation,
                    self.completeness.value,
                    self.venue_id,
                    self.account_id,
                    self.adapter_id,
                    self.adapter_version,
                    self.query_sha256,
                    self.attempt_id,
                    self.started_at,
                    self.finished_at,
                    self.pages,
                    self.rows_observed,
                    self.failure_code,
                )
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairPagedReadResult:
    items: tuple[BetfairCurrentOrderObservation | BetfairClearedOrderObservation, ...]
    witness: BetfairReadCompletenessWitness

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise BetfairReadOnlyError("Betfair paged result items must be a tuple")
        if not isinstance(self.witness, BetfairReadCompletenessWitness):
            raise BetfairReadOnlyError("Betfair paged result requires a completeness witness")
        if len(self.items) != self.witness.rows_observed:
            raise BetfairReadOnlyError(
                "Betfair paged result rows do not match completeness evidence"
            )

    def _fingerprint(self) -> str:
        return sha256(
            repr(("paged", self.items, self.witness._fingerprint())).encode("utf-8")
        ).hexdigest()

    def _assert_issued(self) -> None:
        raise BetfairReadOnlyError(
            "Betfair paged result issuance authority is not installed"
        )

    @property
    def authoritative_empty(self) -> bool:
        if self.items:
            return False
        try:
            self._assert_issued()
            self.witness.assert_authoritative()
        except BetfairReadOnlyError:
            return False
        return True

    def assert_complete(
        self,
    ) -> tuple[BetfairCurrentOrderObservation | BetfairClearedOrderObservation, ...]:
        self._assert_issued()
        self.witness.assert_authoritative()
        return self.items


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairValueReadResult:
    value: BetfairAccountFundsObservation | BetfairAccountDetailsObservation | None
    witness: BetfairReadCompletenessWitness

    def __post_init__(self) -> None:
        if not isinstance(self.witness, BetfairReadCompletenessWitness):
            raise BetfairReadOnlyError("Betfair value result requires a completeness witness")
        expected_rows = 0 if self.value is None else 1
        if self.witness.rows_observed != expected_rows:
            raise BetfairReadOnlyError(
                "Betfair value result rows do not match completeness evidence"
            )

    def _fingerprint(self) -> str:
        return sha256(
            repr(("value", self.value, self.witness._fingerprint())).encode("utf-8")
        ).hexdigest()

    def _assert_issued(self) -> None:
        raise BetfairReadOnlyError(
            "Betfair value result issuance authority is not installed"
        )

    def assert_complete(self) -> BetfairAccountFundsObservation | BetfairAccountDetailsObservation:
        self._assert_issued()
        self.witness.assert_authoritative()
        if self.value is None:
            raise BetfairReadOnlyError("complete scalar read unexpectedly lacks a value")
        return self.value


def _prepare_issuance_authority():
    """Build closure-private issuance state and return the one-time observer installer.

    Positive completeness/result issuance is intentionally not exposed as a module
    function or writable module registry.  The only mutating capability is captured
    by wrappers installed around the canonical observer acquisition methods below.
    """

    lock = Lock()
    issued_witnesses: dict[int, tuple[str, bool]] = {}
    issued_results: dict[int, str] = {}
    active_observers: dict[int, int] = {}

    def drop_witness(identity: int) -> None:
        with lock:
            issued_witnesses.pop(identity, None)

    def drop_result(identity: int) -> None:
        with lock:
            issued_results.pop(identity, None)

    def witness_assert_issued(self: BetfairReadCompletenessWitness) -> None:
        with lock:
            issued = issued_witnesses.get(id(self))
        if issued is None or issued[0] != self._fingerprint():
            raise BetfairReadOnlyError(
                "Betfair completeness witness was not issued by the observer"
            )

    def witness_assert_authoritative(self: BetfairReadCompletenessWitness) -> None:
        if (
            self.completeness
            is not BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW
        ):
            raise BetfairReadOnlyError(
                f"Betfair read is not complete: {self.completeness.value}"
            )
        witness_assert_issued(self)
        with lock:
            issued = issued_witnesses.get(id(self))
        if issued is None or not issued[1]:
            raise BetfairReadOnlyError(
                "Betfair completeness witness lacks product-owned provider origin"
            )

    def result_assert_issued(
        self: BetfairPagedReadResult | BetfairValueReadResult,
    ) -> None:
        with lock:
            issued = issued_results.get(id(self))
        if issued != self._fingerprint():
            kind = "paged" if isinstance(self, BetfairPagedReadResult) else "value"
            raise BetfairReadOnlyError(
                f"Betfair {kind} result was not issued by the observer"
            )

    # Freeze the verification readers directly onto the evidence/result types.
    # Their closure state is read-only to callers; ordinary module rebinding cannot
    # replace the issuance registry or its truth predicate.
    BetfairReadCompletenessWitness.assert_issued = witness_assert_issued
    BetfairReadCompletenessWitness.assert_authoritative = witness_assert_authoritative
    BetfairPagedReadResult._assert_issued = result_assert_issued
    BetfairValueReadResult._assert_issued = result_assert_issued

    def enter(observer: object) -> None:
        identity = id(observer)
        with lock:
            active_observers[identity] = active_observers.get(identity, 0) + 1

    def leave(observer: object) -> None:
        identity = id(observer)
        with lock:
            depth = active_observers.get(identity, 0)
            if depth <= 1:
                active_observers.pop(identity, None)
            else:
                active_observers[identity] = depth - 1

    def is_active(observer: object) -> bool:
        with lock:
            return active_observers.get(id(observer), 0) > 0

    def issue_witness(
        observer: object,
        witness: BetfairReadCompletenessWitness,
        *,
        authoritative_origin: bool,
    ) -> BetfairReadCompletenessWitness:
        if type(authoritative_origin) is not bool:
            raise TypeError("authoritative_origin must be bool")
        if not is_active(observer):
            return witness
        identity = id(witness)
        with lock:
            issued_witnesses[identity] = (
                witness._fingerprint(),
                authoritative_origin,
            )
        weakref.finalize(witness, drop_witness, identity)
        return witness

    def issue_result(
        observer: object,
        result: BetfairPagedReadResult | BetfairValueReadResult,
    ) -> BetfairPagedReadResult | BetfairValueReadResult:
        if not is_active(observer):
            return result
        identity = id(result)
        with lock:
            issued_results[identity] = result._fingerprint()
        weakref.finalize(result, drop_result, identity)
        return result

    def install(observer_type: type) -> None:
        original_witness = observer_type._witness
        original_paged_result = observer_type._paged_result
        original_read_value = observer_type._read_value

        def guarded_witness(self, *args, **kwargs):
            witness = original_witness(self, *args, **kwargs)
            return issue_witness(
                self,
                witness,
                authoritative_origin=self._product_origin_is_intact(),
            )

        def guarded_paged_result(self, *args, **kwargs):
            result = original_paged_result(self, *args, **kwargs)
            return issue_result(self, result)

        def guarded_read_value(self, *args, **kwargs):
            result = original_read_value(self, *args, **kwargs)
            return issue_result(self, result)

        observer_type._witness = guarded_witness
        observer_type._paged_result = guarded_paged_result
        observer_type._read_value = guarded_read_value

        def wrap_public(method):
            def guarded(self, *args, **kwargs):
                enter(self)
                try:
                    return method(self, *args, **kwargs)
                finally:
                    leave(self)
            return guarded

        for name in (
            "read_account_funds",
            "read_account_details",
            "read_current_orders",
            "read_cleared_orders",
        ):
            setattr(observer_type, name, wrap_public(getattr(observer_type, name)))

    return install


_INSTALL_OBSERVER_ISSUANCE = _prepare_issuance_authority()
del _prepare_issuance_authority


class BetfairReadCompletenessObserver:
    """Acquire query-bound read evidence through the one canonical Betfair client."""

    def __init__(
        self,
        client: BetfairReadOnlyClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(client) is not BetfairReadOnlyClient:
            raise TypeError("client must be exact BetfairReadOnlyClient")
        self._client = client
        self._venue_id = _text(getattr(client, "_venue_id", None), "client venue_id")
        self._account_id = _text(getattr(client, "_account_id", None), "client account_id")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Positive completeness is chronology-bearing authority.  Only the
        # observer-owned default clock may participate in that authority; an
        # injected clock remains useful for deterministic structural tests and
        # degraded diagnostics, but it can never mint a positive witness.
        self._product_clock = self._clock if clock is None else None
        self._attempt_lock = Lock()
        self._attempt_sequence = 0

    def read_account_funds(self) -> BetfairValueReadResult:
        self._require_canonical_client_read_dispatch()
        return self._read_value("getAccountFunds", {}, _CANONICAL_READ_ACCOUNT_FUNDS)

    def read_account_details(self) -> BetfairValueReadResult:
        self._require_canonical_client_read_dispatch()
        return self._read_value("getAccountDetails", {}, _CANONICAL_READ_ACCOUNT_DETAILS)

    def read_current_orders(
        self,
        *,
        page_size: int = 1000,
        max_pages: int = 100,
        customer_order_refs: tuple[str, ...] | None = None,
        market_ids: tuple[str, ...] | None = None,
    ) -> BetfairPagedReadResult:
        _positive_int(page_size, "page_size")
        _positive_int(max_pages, "max_pages")
        query = {
            "operation": "listCurrentOrders",
            "orderProjection": "ALL",
            "page_size": page_size,
            "customer_order_refs": customer_order_refs,
            "market_ids": market_ids,
        }
        started, query_sha, attempt_id = self._start(query)
        items: list[BetfairCurrentOrderObservation] = []
        seen: set[str] = set()
        pages: list[tuple[int, int, bool, str]] = []
        offset = 0
        for _ in range(max_pages):
            self._require_canonical_client_read_dispatch()
            try:
                page = _CANONICAL_READ_CURRENT_ORDERS_PAGE(
                    self._client,
                    from_record=offset,
                    record_count=page_size,
                    customer_order_refs=customer_order_refs,
                    market_ids=market_ids,
                )
            except BetfairReadOnlyError as exc:
                completeness, code = _classify_failure(exc, partial=bool(pages))
                return self._paged_result(
                    "listCurrentOrders",
                    query_sha,
                    attempt_id,
                    started,
                    pages,
                    items,
                    completeness,
                    code,
                )
            pages.append(
                (
                    page.from_record,
                    page.record_count,
                    page.more_available,
                    page.evidence.source_payload_sha256,
                )
            )
            if not _extend_unique(items, seen, page.orders):
                return self._paged_result(
                    "listCurrentOrders",
                    query_sha,
                    attempt_id,
                    started,
                    pages,
                    items,
                    BetfairObservationCompleteness.PARTIAL,
                    "cross_page_duplicate",
                )
            if not page.more_available:
                completeness, failure_code = _provider_end_completeness(pages)
                return self._paged_result(
                    "listCurrentOrders",
                    query_sha,
                    attempt_id,
                    started,
                    pages,
                    items,
                    completeness,
                    failure_code,
                )
            if not page.orders:
                return self._paged_result(
                    "listCurrentOrders",
                    query_sha,
                    attempt_id,
                    started,
                    pages,
                    items,
                    BetfairObservationCompleteness.PARTIAL,
                    "pagination_cannot_advance",
                )
            offset += len(page.orders)
        return self._paged_result(
            "listCurrentOrders",
            query_sha,
            attempt_id,
            started,
            pages,
            items,
            BetfairObservationCompleteness.PARTIAL,
            "pagination_limit",
        )

    def read_cleared_orders(
        self,
        *,
        settled_from: str | None = None,
        bet_status: str = "SETTLED",
        page_size: int = 1000,
        max_pages: int = 100,
        customer_order_refs: tuple[str, ...] | None = None,
        market_ids: tuple[str, ...] | None = None,
    ) -> BetfairPagedReadResult:
        _positive_int(page_size, "page_size")
        _positive_int(max_pages, "max_pages")
        query = {
            "operation": "listClearedOrders",
            "bet_status": bet_status,
            "groupBy": "BET",
            "settled_from": settled_from,
            "page_size": page_size,
            "customer_order_refs": customer_order_refs,
            "market_ids": market_ids,
        }
        started, query_sha, attempt_id = self._start(query)
        items: list[BetfairClearedOrderObservation] = []
        seen: set[str] = set()
        pages: list[tuple[int, int, bool, str]] = []
        offset = 0
        for _ in range(max_pages):
            self._require_canonical_client_read_dispatch()
            try:
                page = _CANONICAL_READ_CLEARED_ORDERS_PAGE(
                    self._client,
                    from_record=offset,
                    record_count=page_size,
                    settled_from=settled_from,
                    bet_status=bet_status,
                    customer_order_refs=customer_order_refs,
                    market_ids=market_ids,
                )
            except BetfairReadOnlyError as exc:
                completeness, code = _classify_failure(exc, partial=bool(pages))
                return self._paged_result(
                    "listClearedOrders",
                    query_sha,
                    attempt_id,
                    started,
                    pages,
                    items,
                    completeness,
                    code,
                )
            pages.append(
                (
                    page.from_record,
                    page.record_count,
                    page.more_available,
                    page.evidence.source_payload_sha256,
                )
            )
            if not _extend_unique(items, seen, page.orders):
                return self._paged_result(
                    "listClearedOrders",
                    query_sha,
                    attempt_id,
                    started,
                    pages,
                    items,
                    BetfairObservationCompleteness.PARTIAL,
                    "cross_page_duplicate",
                )
            if not page.more_available:
                completeness, failure_code = _provider_end_completeness(pages)
                return self._paged_result(
                    "listClearedOrders",
                    query_sha,
                    attempt_id,
                    started,
                    pages,
                    items,
                    completeness,
                    failure_code,
                )
            if not page.orders:
                return self._paged_result(
                    "listClearedOrders",
                    query_sha,
                    attempt_id,
                    started,
                    pages,
                    items,
                    BetfairObservationCompleteness.PARTIAL,
                    "pagination_cannot_advance",
                )
            offset += len(page.orders)
        return self._paged_result(
            "listClearedOrders",
            query_sha,
            attempt_id,
            started,
            pages,
            items,
            BetfairObservationCompleteness.PARTIAL,
            "pagination_limit",
        )

    def _read_value(
        self,
        operation: str,
        query: dict[str, object],
        reader: Callable[[BetfairReadOnlyClient], BetfairAccountFundsObservation | BetfairAccountDetailsObservation],
    ) -> BetfairValueReadResult:
        query_payload = {"operation": operation, **query}
        started, query_sha, attempt_id = self._start(query_payload)
        try:
            value = reader(self._client)
        except BetfairReadOnlyError as exc:
            completeness, code = _classify_failure(exc, partial=False)
            witness = self._witness(
                operation,
                query_sha,
                attempt_id,
                started,
                (),
                0,
                completeness,
                code,
            )
            return BetfairValueReadResult(None, witness)
        page = (
            0,
            1,
            False,
            value.evidence.source_payload_sha256,
        )
        witness = self._witness(
            operation,
            query_sha,
            attempt_id,
            started,
            (page,),
            1,
            BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
            None,
        )
        return BetfairValueReadResult(value, witness)

    def _paged_result(
        self,
        operation: str,
        query_sha: str,
        attempt_id: str,
        started: str,
        pages: list[tuple[int, int, bool, str]],
        items: list[BetfairCurrentOrderObservation] | list[BetfairClearedOrderObservation],
        completeness: BetfairObservationCompleteness,
        failure_code: str | None,
    ) -> BetfairPagedReadResult:
        witness = self._witness(
            operation,
            query_sha,
            attempt_id,
            started,
            tuple(pages),
            len(items),
            completeness,
            failure_code,
        )
        return BetfairPagedReadResult(tuple(items), witness)

    def _witness(
        self,
        operation: str,
        query_sha: str,
        attempt_id: str,
        started: str,
        pages: tuple[tuple[int, int, bool, str], ...],
        rows_observed: int,
        completeness: BetfairObservationCompleteness,
        failure_code: str | None,
    ) -> BetfairReadCompletenessWitness:
        witness = BetfairReadCompletenessWitness(
            operation,
            completeness,
            self._venue_id,
            self._account_id,
            ADAPTER_ID,
            ADAPTER_VERSION,
            query_sha,
            attempt_id,
            started,
            self._now(),
            pages,
            rows_observed,
            failure_code,
        )
        return witness

    def _client_read_dispatch_is_intact(self) -> bool:
        """Reject caller-shadowed or rebound provider read executables."""

        try:
            instance_dict = vars(self._client)
        except TypeError:
            return False
        for name, canonical, canonical_code in _CANONICAL_READ_DISPATCH:
            if name in instance_dict:
                return False
            if getattr(BetfairReadOnlyClient, name, None) is not canonical:
                return False
            if getattr(canonical, "__code__", None) is not canonical_code:
                return False
        return True

    def _require_canonical_client_read_dispatch(self) -> None:
        if not self._client_read_dispatch_is_intact():
            raise BetfairReadOnlyError(
                "Betfair completeness client read dispatch changed"
            )

    def _product_origin_is_intact(self) -> bool:
        """Require the already-owned canonical client + observer time origin.

        The campaign provider-scope authority owns the canonical Betfair client
        origin registry.  Completeness consumes that registry read-only instead
        of inventing a second transport/credential authority.  This check runs
        after provider I/O, immediately before issuance, so swapping any bound
        client component or the observer clock during the read fails closed.
        """

        if (
            self._product_clock is None
            or self._clock is not self._product_clock
            or not self._client_read_dispatch_is_intact()
        ):
            return False
        try:
            origin = _client_origin._CANONICAL_CLIENT_ORIGINS.get(self._client)
        except (AttributeError, TypeError):
            return False
        return (
            origin is not None
            and self._client._transport is origin.transport
            and self._client._clock is origin.clock
            and self._client._credentials is origin.credentials
            and type(self._client._transport) is UrllibBetfairHttpTransport
        )

    def _start(self, query: dict[str, object]) -> tuple[str, str, str]:
        started = self._now()
        query_sha = _canonical_sha(
            {
                "schema": "autosport.betfair_read_query_scope.v2",
                "venue_id": self._venue_id,
                "account_id": self._account_id,
                "adapter_id": ADAPTER_ID,
                "adapter_version": ADAPTER_VERSION,
                "query": query,
            }
        )
        with self._attempt_lock:
            self._attempt_sequence += 1
            sequence = self._attempt_sequence
        attempt_id = _canonical_sha(
            {
                "schema": "autosport.betfair_read_attempt",
                "query_sha256": query_sha,
                "started_at": started,
                "sequence": sequence,
            }
        )
        return started, query_sha, attempt_id

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BetfairReadOnlyError("completeness clock must return timezone-aware datetime")
        return value.isoformat()


_INSTALL_OBSERVER_ISSUANCE(BetfairReadCompletenessObserver)
del _INSTALL_OBSERVER_ISSUANCE


def _provider_end_completeness(
    pages: list[tuple[int, int, bool, str]],
) -> tuple[BetfairObservationCompleteness, str | None]:
    """Grant global completeness only to one provider response.

    Betfair's offset-paginated read operations expose no provider snapshot/version
    token binding multiple calls into one atomic account-state image.  A later
    provider-end marker therefore proves traversal exhaustion, not that rows could
    not shift across offsets while the sweep was in progress.
    """
    if len(pages) == 1:
        return (
            BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
            None,
        )
    return (
        BetfairObservationCompleteness.PARTIAL,
        "cross_call_snapshot_unproven",
    )


def _extend_unique(target: list[object], seen: set[str], rows: tuple[object, ...]) -> bool:
    for row in rows:
        bet_id = getattr(row, "bet_id", None)
        if not isinstance(bet_id, str) or not bet_id:
            raise BetfairReadOnlyError("Betfair row lacks canonical bet_id")
        if bet_id in seen:
            return False
        seen.add(bet_id)
        target.append(row)
    return True


def _classify_failure(
    exc: BetfairReadOnlyError,
    *,
    partial: bool,
) -> tuple[BetfairObservationCompleteness, str]:
    completeness, code = _classify_terminal_failure(exc)
    if partial:
        return BetfairObservationCompleteness.PARTIAL, f"partial_{code}"
    return completeness, code


def _classify_terminal_failure(
    exc: BetfairReadOnlyError,
) -> tuple[BetfairObservationCompleteness, str]:
    """Classify the terminal provider/transport cause without losing partialness."""

    provider_error_code = getattr(exc, "provider_error_code", None)
    if provider_error_code is not None:
        if provider_error_code in {
            "TOO_MANY_REQUESTS",
            "SERVICE_BUSY",
            "TIMEOUT_ERROR",
        }:
            return (
                BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT,
                "provider_transient",
            )
        if provider_error_code in {
            "INVALID_SESSION_INFORMATION",
            "INVALID_APP_KEY",
            "NO_SESSION",
            "NO_APP_KEY",
        }:
            return (
                BetfairObservationCompleteness.AUTH_INVALID_OR_EXPIRED,
                "provider_auth",
            )
        # A present typed provider code is the semantic authority. Unknown values
        # fail closed instead of falling back to provider-controlled message text.
        return (
            BetfairObservationCompleteness.INVALID_REQUEST_OR_CONTRACT,
            "provider_contract",
        )

    # Legacy fallback is intentionally limited to errors with no typed Betfair
    # provider semantic code (transport/HTTP failures and pre-#1259 clients).
    message = str(exc).upper()
    if any(
        marker in message
        for marker in (
            "TOO_MANY_REQUESTS",
            "SERVICE_BUSY",
            "TIMEOUT_ERROR",
            "NETWORK REQUEST FAILED",
            "STATUS 429",
            "STATUS 500",
            "STATUS 502",
            "STATUS 503",
            "STATUS 504",
        )
    ):
        return BetfairObservationCompleteness.UNAVAILABLE_TRANSIENT, "provider_transient"
    if any(
        marker in message
        for marker in (
            "INVALID_SESSION_INFORMATION",
            "INVALID_APP_KEY",
            "NO_SESSION",
            "NO_APP_KEY",
            "STATUS 401",
            "STATUS 403",
        )
    ):
        return BetfairObservationCompleteness.AUTH_INVALID_OR_EXPIRED, "provider_auth"
    return BetfairObservationCompleteness.INVALID_REQUEST_OR_CONTRACT, "provider_contract"


def _canonical_sha(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairReadOnlyError("Betfair completeness query is not canonical JSON") from exc
    return sha256(encoded).hexdigest()


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BetfairReadOnlyError(f"{field} must be a positive integer")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairReadOnlyError(f"{field} must be non-empty canonical text")
    return value


def _sha(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise BetfairReadOnlyError(f"{field} must be lowercase SHA-256 hex")
    return text


def _time(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairReadOnlyError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairReadOnlyError(f"{field} must be timezone-aware")
    return parsed
