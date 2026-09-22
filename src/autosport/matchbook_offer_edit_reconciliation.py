from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Iterable


class MatchbookOfferEditReconciliationError(ValueError):
    """Raised when Matchbook offer-edit evidence cannot be reconciled safely."""


class MatchbookOfferEditStatus(str, Enum):
    APPLIED = "applied"
    DELAYED = "delayed"
    FAILED = "failed"


class MatchbookOfferEditFailureReason(str, Enum):
    MARKET_STATUS = "market_status"
    RUNNER_STATUS = "runner_status"
    OFFER_STATUS = "offer_status"
    OFFER_ODDS_STAKE = "offer_odds_stake"
    DELAY_TIMEOUT = "delay_timeout"
    SERVER_ERROR = "server_error"


class MatchbookOfferEditTruth(str, Enum):
    PENDING_DELAY = "pending_delay"
    APPLIED_OBSERVED = "applied_observed"
    FAILED_OBSERVED = "failed_observed"


class MatchbookOfferEditRetryDisposition(str, Enum):
    RECONCILE_BEFORE_ANY_NEW_EDIT = "reconcile_before_any_new_edit"
    WAIT_FOR_DELAYED_EDIT = "wait_for_delayed_edit"
    DO_NOT_REPEAT_SAME_EDIT = "do_not_repeat_same_edit"


class MatchbookOfferEditIdentityDisposition(str, Enum):
    EXACT_PROVIDER_EDIT_ID = "exact_provider_edit_id"
    UNKNOWN_NO_EDIT_ID_FAIL_CLOSED = "unknown_no_edit_id_fail_closed"


def _canonical_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise MatchbookOfferEditReconciliationError(f"{field} must be canonical text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise MatchbookOfferEditReconciliationError(f"{field} must be UTF-8") from exc
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise MatchbookOfferEditReconciliationError(f"{field} must be a positive integer")
    return value


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise MatchbookOfferEditReconciliationError(f"{field} must be a finite Decimal")
    if positive and value <= 0:
        raise MatchbookOfferEditReconciliationError(f"{field} must be positive")
    if not positive and value < 0:
        raise MatchbookOfferEditReconciliationError(f"{field} must be non-negative")
    return value


def _aware_utc(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise MatchbookOfferEditReconciliationError(
            f"{field} must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _sha256(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise MatchbookOfferEditReconciliationError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return value


def _canonical_decimal_text(value: Decimal) -> str:
    # str(Decimal) is exact and independent of the active decimal context.
    return str(value)


@dataclass(frozen=True, slots=True)
class MatchbookOfferEditIntent:
    """Exact edit request semantics; this object never proves provider acceptance."""

    account_context_id: str
    offer_id: int
    current_odds: Decimal
    current_stake: Decimal
    new_odds: Decimal
    new_stake: Decimal
    requested_at: datetime

    def __post_init__(self) -> None:
        _canonical_text(self.account_context_id, "account_context_id")
        _positive_int(self.offer_id, "offer_id")
        _decimal(self.current_odds, "current_odds", positive=True)
        _decimal(self.current_stake, "current_stake", positive=True)
        _decimal(self.new_odds, "new_odds", positive=True)
        _decimal(self.new_stake, "new_stake", positive=True)
        _aware_utc(self.requested_at, "requested_at")
        if self.current_odds == self.new_odds and self.current_stake == self.new_stake:
            raise MatchbookOfferEditReconciliationError(
                "offer edit must change odds or stake"
            )

    @property
    def provider_write_authority(self) -> bool:
        return False

    def fingerprint(self) -> str:
        payload = {
            "account_context_id": self.account_context_id,
            "current_odds": _canonical_decimal_text(self.current_odds),
            "current_stake": _canonical_decimal_text(self.current_stake),
            "new_odds": _canonical_decimal_text(self.new_odds),
            "new_stake": _canonical_decimal_text(self.new_stake),
            "offer_id": self.offer_id,
            "requested_at": _aware_utc(
                self.requested_at, "requested_at"
            ).isoformat(timespec="microseconds"),
            "schema_version": 1,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchbookOfferEditReadback:
    """Provider GET readback for one exact offer_id + offer_edit_id."""

    account_context_id: str
    offer_id: int
    offer_edit_id: int
    status: MatchbookOfferEditStatus
    captured_at: datetime
    raw_response_sha256: str
    delay_seconds: Decimal | None = None
    failure_reason: MatchbookOfferEditFailureReason | None = None

    def __post_init__(self) -> None:
        _canonical_text(self.account_context_id, "account_context_id")
        _positive_int(self.offer_id, "offer_id")
        _positive_int(self.offer_edit_id, "offer_edit_id")
        if type(self.status) is not MatchbookOfferEditStatus:
            raise MatchbookOfferEditReconciliationError(
                "status must be MatchbookOfferEditStatus"
            )
        _aware_utc(self.captured_at, "captured_at")
        _sha256(self.raw_response_sha256, "raw_response_sha256")

        if self.delay_seconds is not None:
            _decimal(self.delay_seconds, "delay_seconds")
        if self.failure_reason is not None and type(self.failure_reason) is not MatchbookOfferEditFailureReason:
            raise MatchbookOfferEditReconciliationError(
                "failure_reason must be MatchbookOfferEditFailureReason"
            )

        if self.status is MatchbookOfferEditStatus.FAILED:
            if self.failure_reason is None:
                raise MatchbookOfferEditReconciliationError(
                    "failed edit requires provider failure_reason"
                )
        elif self.failure_reason is not None:
            raise MatchbookOfferEditReconciliationError(
                "non-failed edit cannot carry failure_reason"
            )

        if self.status is MatchbookOfferEditStatus.DELAYED:
            if self.delay_seconds is None or self.delay_seconds <= 0:
                raise MatchbookOfferEditReconciliationError(
                    "delayed edit requires positive delay_seconds"
                )

    @property
    def truth(self) -> MatchbookOfferEditTruth:
        if self.status is MatchbookOfferEditStatus.DELAYED:
            return MatchbookOfferEditTruth.PENDING_DELAY
        if self.status is MatchbookOfferEditStatus.APPLIED:
            return MatchbookOfferEditTruth.APPLIED_OBSERVED
        return MatchbookOfferEditTruth.FAILED_OBSERVED

    @property
    def provider_write_authority(self) -> bool:
        return False

    @property
    def settlement_authority(self) -> bool:
        return False

    @property
    def retry_authority(self) -> bool:
        return False

    def fingerprint(self) -> str:
        payload = {
            "account_context_id": self.account_context_id,
            "captured_at": _aware_utc(
                self.captured_at, "captured_at"
            ).isoformat(timespec="microseconds"),
            "delay_seconds": (
                None
                if self.delay_seconds is None
                else _canonical_decimal_text(self.delay_seconds)
            ),
            "failure_reason": (
                None if self.failure_reason is None else self.failure_reason.value
            ),
            "offer_edit_id": self.offer_edit_id,
            "offer_id": self.offer_id,
            "raw_response_sha256": self.raw_response_sha256,
            "schema_version": 1,
            "status": self.status.value,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MatchbookOfferEditEvidence:
    """Binds one request intent to a provider-known edit identity and readback."""

    intent: MatchbookOfferEditIntent
    readback: MatchbookOfferEditReadback

    def __post_init__(self) -> None:
        if type(self.intent) is not MatchbookOfferEditIntent:
            raise MatchbookOfferEditReconciliationError("invalid edit intent")
        if type(self.readback) is not MatchbookOfferEditReadback:
            raise MatchbookOfferEditReconciliationError("invalid edit readback")
        if self.intent.account_context_id != self.readback.account_context_id:
            raise MatchbookOfferEditReconciliationError(
                "edit evidence crossed account context"
            )
        if self.intent.offer_id != self.readback.offer_id:
            raise MatchbookOfferEditReconciliationError(
                "edit evidence crossed offer identity"
            )
        if _aware_utc(self.readback.captured_at, "captured_at") < _aware_utc(
            self.intent.requested_at, "requested_at"
        ):
            raise MatchbookOfferEditReconciliationError(
                "edit readback predates request intent"
            )

    @property
    def provider_write_authority(self) -> bool:
        return False

    @property
    def edited_terms_are_observed_applied(self) -> bool:
        return self.readback.status is MatchbookOfferEditStatus.APPLIED

    @property
    def edited_terms_are_pending(self) -> bool:
        return self.readback.status is MatchbookOfferEditStatus.DELAYED


def identity_disposition(*, offer_edit_id: object | None) -> MatchbookOfferEditIdentityDisposition:
    """Collection/search similarity is never a substitute for an exact provider edit id."""
    if offer_edit_id is None:
        return MatchbookOfferEditIdentityDisposition.UNKNOWN_NO_EDIT_ID_FAIL_CLOSED
    _positive_int(offer_edit_id, "offer_edit_id")
    return MatchbookOfferEditIdentityDisposition.EXACT_PROVIDER_EDIT_ID


def retry_disposition(
    *,
    transport_exception: bool = False,
    http_status: int | None = None,
    readback: MatchbookOfferEditReadback | None = None,
) -> MatchbookOfferEditRetryDisposition:
    """Transport/HTTP/failure evidence never authorizes blind repetition of an edit."""
    if type(transport_exception) is not bool:
        raise MatchbookOfferEditReconciliationError(
            "transport_exception must be bool"
        )
    if http_status is not None and (
        type(http_status) is not int or not 100 <= http_status <= 599
    ):
        raise MatchbookOfferEditReconciliationError("invalid HTTP status")
    if readback is not None and type(readback) is not MatchbookOfferEditReadback:
        raise MatchbookOfferEditReconciliationError("invalid edit readback")

    if readback is None:
        return MatchbookOfferEditRetryDisposition.RECONCILE_BEFORE_ANY_NEW_EDIT
    if readback.status is MatchbookOfferEditStatus.DELAYED:
        return MatchbookOfferEditRetryDisposition.WAIT_FOR_DELAYED_EDIT
    return MatchbookOfferEditRetryDisposition.DO_NOT_REPEAT_SAME_EDIT


def reconcile_offer_edit_replay(
    observations: Iterable[MatchbookOfferEditReadback],
) -> dict[int, MatchbookOfferEditReadback]:
    """Replay provider edit readbacks without inventing correction or retry authority."""
    latest: dict[int, MatchbookOfferEditReadback] = {}
    terminal = {MatchbookOfferEditStatus.APPLIED, MatchbookOfferEditStatus.FAILED}

    for item in observations:
        if type(item) is not MatchbookOfferEditReadback:
            raise MatchbookOfferEditReconciliationError(
                "invalid edit replay observation"
            )
        prior = latest.get(item.offer_edit_id)
        if prior is None:
            latest[item.offer_edit_id] = item
            continue

        if item.account_context_id != prior.account_context_id:
            raise MatchbookOfferEditReconciliationError(
                "offer edit replay crossed account context"
            )
        if item.offer_id != prior.offer_id:
            raise MatchbookOfferEditReconciliationError(
                "offer edit replay crossed offer identity"
            )

        old_time = _aware_utc(prior.captured_at, "captured_at")
        new_time = _aware_utc(item.captured_at, "captured_at")
        if new_time < old_time:
            raise MatchbookOfferEditReconciliationError(
                "offer edit capture time moved backwards"
            )
        if new_time == old_time:
            if item.fingerprint() != prior.fingerprint():
                raise MatchbookOfferEditReconciliationError(
                    "conflicting same-time offer edit readback"
                )
            continue

        if prior.status in terminal and item.status is not prior.status:
            raise MatchbookOfferEditReconciliationError(
                "terminal offer edit changed status without correction authority"
            )
        if prior.status is MatchbookOfferEditStatus.DELAYED:
            if item.status not in {
                MatchbookOfferEditStatus.DELAYED,
                MatchbookOfferEditStatus.APPLIED,
                MatchbookOfferEditStatus.FAILED,
            }:
                raise MatchbookOfferEditReconciliationError(
                    "invalid delayed offer edit transition"
                )
            if (
                item.status is MatchbookOfferEditStatus.DELAYED
                and item.delay_seconds != prior.delay_seconds
            ):
                raise MatchbookOfferEditReconciliationError(
                    "delayed offer edit changed delay without correction authority"
                )
        elif item.status is prior.status:
            # Repeated terminal readback is allowed. Its raw provider bytes/capture
            # time may differ, but the semantic terminal state may not acquire a
            # different failure reason or delay without an explicit correction path.
            if item.failure_reason != prior.failure_reason:
                raise MatchbookOfferEditReconciliationError(
                    "terminal offer edit failure reason changed"
                )
            if item.delay_seconds != prior.delay_seconds:
                raise MatchbookOfferEditReconciliationError(
                    "terminal offer edit delay evidence changed"
                )

        latest[item.offer_edit_id] = item

    return latest
