from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from threading import Lock
from typing import Iterable
import weakref


class MatchbookOfferReconciliationError(ValueError):
    pass


class MatchbookOfferStatus(str, Enum):
    OPEN = "open"
    CANCELLED = "cancelled"
    EDITED = "edited"
    MATCHED = "matched"
    FLUSHED = "flushed"
    FAILED = "failed"
    DELAYED = "delayed"


class MatchbookOfferTruth(str, Enum):
    DELAYED_PENDING = "delayed_pending"
    OPEN_UNMATCHED = "open_unmatched"
    OPEN_PARTIALLY_MATCHED = "open_partially_matched"
    FULLY_MATCHED = "fully_matched"
    TERMINAL_NO_MATCH = "terminal_no_match"
    TERMINAL_PARTIAL_MATCH = "terminal_partial_match"
    SUBMISSION_FAILED = "submission_failed"


class MatchbookRetryDisposition(str, Enum):
    RECONCILE_BEFORE_RETRY = "reconcile_before_retry"
    DO_NOT_RETRY_ALREADY_OBSERVED = "do_not_retry_already_observed"


class MatchbookSettledBetStatus(str, Enum):
    WIN = "WIN"
    LOSE = "LOSE"
    PUSH = "PUSH"
    PUSH_WIN = "PUSH_WIN"
    PUSH_LOSE = "PUSH_LOSE"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise MatchbookOfferReconciliationError(f"{field} must be canonical text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MatchbookOfferReconciliationError(f"{field} must be UTF-8") from exc
    return value


def _pos_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise MatchbookOfferReconciliationError(f"{field} must be a positive integer")
    return value


def _nonneg_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise MatchbookOfferReconciliationError(f"{field} must be non-negative")
    return value


def _amount(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise MatchbookOfferReconciliationError(f"{field} must be a finite non-negative Decimal")
    return value


def _sha(value: object, field: str) -> str:
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise MatchbookOfferReconciliationError(f"{field} must be lowercase SHA-256 hex")
    return value


def _time(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise MatchbookOfferReconciliationError(f"{field} must be timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class MatchbookOfferReadback:
    """Singular provider readback evidence; never provider-write or settlement authority."""

    account_context_id: str
    offer_id: int
    status: MatchbookOfferStatus
    original_stake: Decimal
    matched_stake: Decimal
    remaining_stake: Decimal
    captured_at: datetime
    raw_response_sha256: str

    def __post_init__(self) -> None:
        _text(self.account_context_id, "account_context_id")
        _pos_int(self.offer_id, "offer_id")
        if type(self.status) is not MatchbookOfferStatus:
            raise MatchbookOfferReconciliationError("status must be MatchbookOfferStatus")
        original = _amount(self.original_stake, "original_stake")
        matched = _amount(self.matched_stake, "matched_stake")
        remaining = _amount(self.remaining_stake, "remaining_stake")
        if original <= 0 or matched > original or remaining > original or matched + remaining > original:
            raise MatchbookOfferReconciliationError("stake amounts are inconsistent")
        _time(self.captured_at, "captured_at")
        _sha(self.raw_response_sha256, "raw_response_sha256")

        if self.status is MatchbookOfferStatus.MATCHED and (matched != original or remaining != 0):
            raise MatchbookOfferReconciliationError("matched status requires full match")
        if self.status in {MatchbookOfferStatus.OPEN, MatchbookOfferStatus.EDITED} and remaining <= 0:
            raise MatchbookOfferReconciliationError("open/edited requires remaining stake")
        if self.status is MatchbookOfferStatus.DELAYED and matched != 0:
            raise MatchbookOfferReconciliationError("delayed cannot prove matched stake")
        if self.status is MatchbookOfferStatus.FAILED and (matched != 0 or remaining != 0):
            raise MatchbookOfferReconciliationError("failed cannot carry exposure")
        if self.status in {MatchbookOfferStatus.CANCELLED, MatchbookOfferStatus.FLUSHED} and remaining != 0:
            raise MatchbookOfferReconciliationError("cancelled/flushed closes unmatched remainder")

    @property
    def truth(self) -> MatchbookOfferTruth:
        if self.status is MatchbookOfferStatus.DELAYED:
            return MatchbookOfferTruth.DELAYED_PENDING
        if self.status in {MatchbookOfferStatus.OPEN, MatchbookOfferStatus.EDITED}:
            return MatchbookOfferTruth.OPEN_PARTIALLY_MATCHED if self.matched_stake else MatchbookOfferTruth.OPEN_UNMATCHED
        if self.status is MatchbookOfferStatus.MATCHED:
            return MatchbookOfferTruth.FULLY_MATCHED
        if self.status in {MatchbookOfferStatus.CANCELLED, MatchbookOfferStatus.FLUSHED}:
            return MatchbookOfferTruth.TERMINAL_PARTIAL_MATCH if self.matched_stake else MatchbookOfferTruth.TERMINAL_NO_MATCH
        return MatchbookOfferTruth.SUBMISSION_FAILED

    @property
    def settlement_authority(self) -> bool:
        return False

    @property
    def provider_write_authority(self) -> bool:
        return False

    @property
    def provider_origin_authoritative(self) -> bool:
        """Whether this exact in-process object was issued by product acquisition."""
        try:
            _assert_provider_readback_issued(self)
        except MatchbookOfferReconciliationError:
            return False
        return True

    def fingerprint(self) -> str:
        payload = {
            "account_context_id": self.account_context_id,
            "captured_at": _time(self.captured_at, "captured_at").isoformat(timespec="microseconds"),
            "matched_stake": str(self.matched_stake),
            "offer_id": self.offer_id,
            "original_stake": str(self.original_stake),
            "raw_response_sha256": self.raw_response_sha256,
            "remaining_stake": str(self.remaining_stake),
            "schema_version": 1,
            "status": self.status.value,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


_ISSUED_READBACK_LOCK = Lock()
_ISSUED_READBACKS: dict[int, str] = {}


def _forget_issued_readback(identity: int) -> None:
    with _ISSUED_READBACK_LOCK:
        _ISSUED_READBACKS.pop(identity, None)


def _issue_provider_readback(readback: MatchbookOfferReadback) -> MatchbookOfferReadback:
    """Internal acquisition seam: issue only after canonical provider-origin validation."""
    if type(readback) is not MatchbookOfferReadback:
        raise MatchbookOfferReconciliationError(
            "provider readback issuance requires exact MatchbookOfferReadback"
        )
    identity = id(readback)
    with _ISSUED_READBACK_LOCK:
        _ISSUED_READBACKS[identity] = readback.fingerprint()
    weakref.finalize(readback, _forget_issued_readback, identity)
    return readback


def _assert_provider_readback_issued(readback: MatchbookOfferReadback) -> None:
    if type(readback) is not MatchbookOfferReadback:
        raise MatchbookOfferReconciliationError(
            "provider readback authority requires exact MatchbookOfferReadback"
        )
    with _ISSUED_READBACK_LOCK:
        issued = _ISSUED_READBACKS.get(id(readback))
    if issued != readback.fingerprint():
        raise MatchbookOfferReconciliationError(
            "Matchbook offer readback lacks product-issued provider origin"
        )


@dataclass(frozen=True, slots=True)
class MatchbookOfferPage:
    """One unaggregated GET /v2/offers page for a fixed query scope."""

    account_context_id: str
    query_scope_sha256: str
    offset: int
    per_page: int
    offers: tuple[MatchbookOfferReadback, ...]
    raw_response_sha256: str

    def __post_init__(self) -> None:
        _text(self.account_context_id, "account_context_id")
        _sha(self.query_scope_sha256, "query_scope_sha256")
        _nonneg_int(self.offset, "offset")
        _pos_int(self.per_page, "per_page")
        _sha(self.raw_response_sha256, "raw_response_sha256")
        if type(self.offers) is not tuple or len(self.offers) > self.per_page:
            raise MatchbookOfferReconciliationError("invalid page rows")
        seen: set[int] = set()
        for item in self.offers:
            if type(item) is not MatchbookOfferReadback or item.account_context_id != self.account_context_id:
                raise MatchbookOfferReconciliationError("page offer context/type mismatch")
            if item.offer_id in seen:
                raise MatchbookOfferReconciliationError("duplicate offer identity within page")
            seen.add(item.offer_id)

    @property
    def terminal_short_page(self) -> bool:
        return len(self.offers) < self.per_page


@dataclass(frozen=True, slots=True)
class MatchbookOfferPageChain:
    """Pagination closure only; it never claims a transactionally frozen provider snapshot."""

    pages: tuple[MatchbookOfferPage, ...]

    def __post_init__(self) -> None:
        if type(self.pages) is not tuple or not self.pages or self.pages[0].offset != 0:
            raise MatchbookOfferReconciliationError("page chain must start at offset 0")
        first = self.pages[0]
        expected = 0
        seen: dict[int, str] = {}
        terminal = False
        for page in self.pages:
            if page.account_context_id != first.account_context_id or page.query_scope_sha256 != first.query_scope_sha256 or page.per_page != first.per_page:
                raise MatchbookOfferReconciliationError("page query identity changed")
            if page.offset != expected:
                raise MatchbookOfferReconciliationError("page offset is not contiguous")
            if terminal:
                raise MatchbookOfferReconciliationError("page follows terminal short page")
            for item in page.offers:
                fp = item.fingerprint()
                if item.offer_id in seen:
                    kind = "conflicting duplicate" if seen[item.offer_id] != fp else "duplicate"
                    raise MatchbookOfferReconciliationError(f"{kind} offer identity across pages")
                seen[item.offer_id] = fp
            expected += len(page.offers)
            terminal = page.terminal_short_page

    @property
    def pagination_closed(self) -> bool:
        return self.pages[-1].terminal_short_page

    @property
    def provider_snapshot_atomicity_proven(self) -> bool:
        return False

    def traversal_absence_observed(self, offer_id: int) -> bool:
        _pos_int(offer_id, "offer_id")
        return self.pagination_closed and all(offer_id != item.offer_id for page in self.pages for item in page.offers)


def reject_aggregated_offer_identity(*, offer_id: object, offer_ids: object) -> int:
    """Aggregated rows expose ids[] and cannot mint one stable reconciliation identity."""
    if offer_ids is not None:
        raise MatchbookOfferReconciliationError("aggregated offer ids are not singular identity")
    return _pos_int(offer_id, "offer_id")


def retry_disposition(
    *,
    transport_exception: bool = False,
    http_status: int | None = None,
    observed_offer: MatchbookOfferReadback | None = None,
    expected_account_context_id: str | None = None,
    expected_offer_id: int | None = None,
) -> MatchbookRetryDisposition:
    """Only exact provider readback for the submitted attempt can suppress retry."""
    if type(transport_exception) is not bool:
        raise MatchbookOfferReconciliationError("transport_exception must be bool")
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        raise MatchbookOfferReconciliationError("invalid HTTP status")
    if observed_offer is not None and type(observed_offer) is not MatchbookOfferReadback:
        raise MatchbookOfferReconciliationError("observed_offer has invalid type")
    expected_account = (
        None
        if expected_account_context_id is None
        else _text(expected_account_context_id, "expected_account_context_id")
    )
    expected_id = (
        None
        if expected_offer_id is None
        else _pos_int(expected_offer_id, "expected_offer_id")
    )
    if observed_offer is not None:
        if (
            expected_account is None
            or expected_id is None
            or observed_offer.account_context_id != expected_account
            or observed_offer.offer_id != expected_id
        ):
            return MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
        if not observed_offer.provider_origin_authoritative:
            return MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
        return MatchbookRetryDisposition.DO_NOT_RETRY_ALREADY_OBSERVED
    return MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY


def require_distinct_settled_status(value: object) -> MatchbookSettledBetStatus:
    if type(value) is not str:
        raise MatchbookOfferReconciliationError("settled status must be text")
    try:
        return MatchbookSettledBetStatus(value)
    except ValueError as exc:
        raise MatchbookOfferReconciliationError("unsupported settled-bet status") from exc


def reconcile_replay(observations: Iterable[MatchbookOfferReadback]) -> dict[int, MatchbookOfferReadback]:
    """Idempotent restart overlap with monotonic matched exposure and terminality."""
    latest: dict[int, MatchbookOfferReadback] = {}
    terminal = {MatchbookOfferStatus.MATCHED, MatchbookOfferStatus.CANCELLED, MatchbookOfferStatus.FLUSHED, MatchbookOfferStatus.FAILED}
    live = {MatchbookOfferStatus.OPEN, MatchbookOfferStatus.EDITED, MatchbookOfferStatus.DELAYED}
    for item in observations:
        if type(item) is not MatchbookOfferReadback:
            raise MatchbookOfferReconciliationError("invalid replay observation")
        prior = latest.get(item.offer_id)
        if prior is None:
            latest[item.offer_id] = item
            continue
        if item.account_context_id != prior.account_context_id:
            raise MatchbookOfferReconciliationError(
                "offer replay crossed account context"
            )
        old_time = _time(prior.captured_at, "captured_at")
        new_time = _time(item.captured_at, "captured_at")
        if new_time < old_time:
            raise MatchbookOfferReconciliationError("capture time moved backwards")
        if new_time == old_time:
            if item.fingerprint() != prior.fingerprint():
                raise MatchbookOfferReconciliationError("conflicting same-time readback")
            continue
        if item.matched_stake < prior.matched_stake:
            raise MatchbookOfferReconciliationError("matched stake decreased")
        if prior.status in terminal and item.status in live:
            raise MatchbookOfferReconciliationError("terminal offer regressed to live")
        if prior.status in terminal and item.status is not prior.status:
            raise MatchbookOfferReconciliationError(
                "terminal offer changed status without correction authority"
            )
        latest[item.offer_id] = item
    # Structural DTO integrity is not provider-origin authority.  A replay may
    # encounter unissued rows while checking contradictions, but no row may
    # survive into authoritative exposure truth unless it was issued by the
    # product acquisition seam in this process.
    for item in latest.values():
        _assert_provider_readback_issued(item)
    return latest
