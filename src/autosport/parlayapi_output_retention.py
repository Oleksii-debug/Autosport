from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


PARLAYAPI_TERMS_POLICY_IDENTITY = "parlayapi-terms+aup@2026-05-07"
PARLAYAPI_TERMS_URL = "https://parlay-api.com/terms"
PARLAYAPI_AUP_URL = "https://api.parlay-api.com/legal/acceptable-use"
LINE_LEVEL_MAX_RETENTION = timedelta(days=90)


class ParlayApiRetentionError(ValueError):
    """Raised when retention evidence is structurally unsafe or unusable."""


def _line_level_retention_deadline(captured_at: datetime) -> datetime:
    try:
        return captured_at + LINE_LEVEL_MAX_RETENTION
    except (OverflowError, ValueError) as exc:
        raise ParlayApiRetentionError(
            "line-level retention exceeds supported datetime range"
        ) from exc


class ParlayApiOutputClass(str, Enum):
    LINE_LEVEL_PRICING = "LINE_LEVEL_PRICING"
    MATCH_RESULT_OR_OTHER_OUTPUT = "MATCH_RESULT_OR_OTHER_OUTPUT"
    DERIVED_NON_RAW = "DERIVED_NON_RAW"


class ParlayApiRetentionState(str, Enum):
    NOT_YET_AVAILABLE = "NOT_YET_AVAILABLE"
    ACQUISITION_AUTHORITY_UNRESOLVED = "ACQUISITION_AUTHORITY_UNRESOLVED"
    RAW_USABLE = "RAW_USABLE"
    DELETE_REQUIRED = "DELETE_REQUIRED"
    LEGAL_REVIEW_REQUIRED = "LEGAL_REVIEW_REQUIRED"
    DERIVED_NON_RAW = "DERIVED_NON_RAW"


@dataclass(frozen=True, slots=True)
class ParlayApiRetentionDecision:
    state: ParlayApiRetentionState
    effective_retention_deadline: datetime | None
    raw_use_allowed: bool
    training_corpus_allowed: bool
    raw_redistribution_allowed: bool
    delete_raw_output: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ParlayApiOutputRetentionEvidence:
    """Retention evidence for one captured ParlayAPI output.

    This authority deliberately stores no provider bytes and offers no tier,
    access-window, or caller-boolean shortcut that can extend line-level pricing
    beyond 90 days. Any future written-consent or redistribution grant must be a
    separate re-resolvable authority, not an unchecked field here.

    ``available_at`` is the first causal provider-availability time known to this
    evidence; an output cannot be captured before that instant. Replay of this exact
    capture is additionally bounded by ``captured_at`` because product knowledge
    cannot predate acquisition. ``policy_identity`` may describe an older historical
    capture so an as-of audit can reconstruct the evidence. New captures should use
    :func:`new_capture_retention_evidence`, which binds the current policy identity.
    """

    output_class: ParlayApiOutputClass
    source_payload_sha256: str
    acquisition_identity_sha256: str
    captured_at: datetime
    available_at: datetime
    policy_identity: str
    observed_cache_control: str | None = None
    internal_retention_until: datetime | None = None
    subscription_terminated_at: datetime | None = None
    subscription_tier: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.output_class, ParlayApiOutputClass):
            raise ParlayApiRetentionError("output_class must be ParlayApiOutputClass")
        _require_sha256(self.source_payload_sha256, "source_payload_sha256")
        _require_sha256(self.acquisition_identity_sha256, "acquisition_identity_sha256")
        _require_aware(self.captured_at, "captured_at")
        _require_aware(self.available_at, "available_at")
        if self.captured_at < self.available_at:
            raise ParlayApiRetentionError("captured_at cannot precede available_at")
        if self.output_class is ParlayApiOutputClass.LINE_LEVEL_PRICING:
            _line_level_retention_deadline(self.captured_at)
        _require_text(self.policy_identity, "policy_identity")
        if self.observed_cache_control is not None:
            _require_text(self.observed_cache_control, "observed_cache_control")
            _cache_control_deadline(
                self.observed_cache_control,
                captured_at=self.captured_at,
            )
        if self.internal_retention_until is not None:
            _require_aware(self.internal_retention_until, "internal_retention_until")
            if self.internal_retention_until < self.captured_at:
                raise ParlayApiRetentionError(
                    "internal_retention_until cannot precede captured_at"
                )
        if self.subscription_terminated_at is not None:
            _require_aware(
                self.subscription_terminated_at,
                "subscription_terminated_at",
            )
        if self.subscription_tier is not None:
            _require_text(self.subscription_tier, "subscription_tier")
        if (
            self.output_class is ParlayApiOutputClass.DERIVED_NON_RAW
            and self.internal_retention_until is not None
        ):
            raise ParlayApiRetentionError(
                "DERIVED_NON_RAW must not masquerade as a raw-output retention grant"
            )

    def effective_retention_deadline(self) -> datetime | None:
        if self.output_class is ParlayApiOutputClass.DERIVED_NON_RAW:
            return None

        candidates: list[datetime] = []
        if self.output_class is ParlayApiOutputClass.LINE_LEVEL_PRICING:
            candidates.append(_line_level_retention_deadline(self.captured_at))
        elif self.internal_retention_until is None:
            return None

        if self.internal_retention_until is not None:
            candidates.append(self.internal_retention_until)

        cache_deadline = _cache_control_deadline(
            self.observed_cache_control,
            captured_at=self.captured_at,
        )
        if cache_deadline is not None:
            candidates.append(cache_deadline)
        if self.subscription_terminated_at is not None:
            candidates.append(self.subscription_terminated_at)

        return min(candidates) if candidates else None

    def evaluate(
        self,
        *,
        as_of: datetime,
        current_policy_identity: str = PARLAYAPI_TERMS_POLICY_IDENTITY,
    ) -> ParlayApiRetentionDecision:
        _require_aware(as_of, "as_of")
        _require_text(current_policy_identity, "current_policy_identity")
        deadline = self.effective_retention_deadline()

        if as_of < self.captured_at:
            return ParlayApiRetentionDecision(
                state=ParlayApiRetentionState.NOT_YET_AVAILABLE,
                effective_retention_deadline=deadline,
                raw_use_allowed=False,
                training_corpus_allowed=False,
                raw_redistribution_allowed=False,
                delete_raw_output=False,
                reason="this provider Output capture did not yet exist at this cutoff",
            )

        # Binding deletion/expiry always wins over a policy-version review. A
        # changed policy must never keep already-expired raw Output around merely
        # because a review is also required.
        if deadline is not None and as_of >= deadline:
            return ParlayApiRetentionDecision(
                state=ParlayApiRetentionState.DELETE_REQUIRED,
                effective_retention_deadline=deadline,
                raw_use_allowed=False,
                training_corpus_allowed=False,
                raw_redistribution_allowed=False,
                delete_raw_output=True,
                reason="raw provider Output reached a binding retention/deletion boundary",
            )

        if current_policy_identity != self.policy_identity:
            return ParlayApiRetentionDecision(
                state=ParlayApiRetentionState.LEGAL_REVIEW_REQUIRED,
                effective_retention_deadline=deadline,
                raw_use_allowed=False,
                training_corpus_allowed=False,
                raw_redistribution_allowed=False,
                delete_raw_output=False,
                reason="current ParlayAPI policy identity differs from capture-time policy",
            )

        if _cache_control_requires_revalidation(self.observed_cache_control):
            return ParlayApiRetentionDecision(
                state=ParlayApiRetentionState.LEGAL_REVIEW_REQUIRED,
                effective_retention_deadline=deadline,
                raw_use_allowed=False,
                training_corpus_allowed=False,
                raw_redistribution_allowed=False,
                delete_raw_output=False,
                reason="cache-control requires revalidation before continued raw use",
            )

        if self.output_class is ParlayApiOutputClass.DERIVED_NON_RAW:
            return ParlayApiRetentionDecision(
                state=ParlayApiRetentionState.DERIVED_NON_RAW,
                effective_retention_deadline=None,
                raw_use_allowed=False,
                training_corpus_allowed=False,
                raw_redistribution_allowed=False,
                delete_raw_output=False,
                reason=(
                    "raw-retention authority cannot prove derived artifact "
                    "non-reconstructability; separate derived-corpus qualification required"
                ),
            )

        if deadline is None:
            return ParlayApiRetentionDecision(
                state=ParlayApiRetentionState.LEGAL_REVIEW_REQUIRED,
                effective_retention_deadline=None,
                raw_use_allowed=False,
                training_corpus_allowed=False,
                raw_redistribution_allowed=False,
                delete_raw_output=False,
                reason="raw Output has no finite product-owned internal retention horizon",
            )

        # The structural evidence object is deliberately not positive acquisition
        # authority. Its hashes and timestamps are caller-supplied, and the current
        # canonical historical acquisition lineage does not yet expose a
        # product-owned, re-resolvable acquisition-clock authority. Granting
        # RAW_USABLE here would let old bytes be re-wrapped with a fresh
        # acquisition identity/timestamp and restart the retention clock.
        #
        # Keep the computed deadline available for deletion/audit, but deny raw or
        # corpus use until a future canonical resolver can re-establish the exact
        # provider capture identity and acquisition time from durable product truth.
        return ParlayApiRetentionDecision(
            state=ParlayApiRetentionState.ACQUISITION_AUTHORITY_UNRESOLVED,
            effective_retention_deadline=deadline,
            raw_use_allowed=False,
            training_corpus_allowed=False,
            raw_redistribution_allowed=False,
            delete_raw_output=False,
            reason=(
                "caller-supplied retention evidence cannot prove canonical "
                "product-owned acquisition identity/time"
            ),
        )

    def require_raw_use(
        self,
        *,
        as_of: datetime,
        current_policy_identity: str = PARLAYAPI_TERMS_POLICY_IDENTITY,
    ) -> None:
        decision = self.evaluate(
            as_of=as_of,
            current_policy_identity=current_policy_identity,
        )
        if not decision.raw_use_allowed:
            raise ParlayApiRetentionError(
                f"raw ParlayAPI Output use blocked: {decision.state.value}: {decision.reason}"
            )

    def require_training_corpus_use(
        self,
        *,
        as_of: datetime,
        current_policy_identity: str = PARLAYAPI_TERMS_POLICY_IDENTITY,
    ) -> None:
        decision = self.evaluate(
            as_of=as_of,
            current_policy_identity=current_policy_identity,
        )
        if not decision.training_corpus_allowed:
            raise ParlayApiRetentionError(
                "ParlayAPI Output cannot enter training/replay corpus: "
                f"{decision.state.value}: {decision.reason}"
            )


def new_capture_retention_evidence(
    *,
    output_class: ParlayApiOutputClass,
    source_payload_sha256: str,
    acquisition_identity_sha256: str,
    captured_at: datetime,
    available_at: datetime,
    observed_cache_control: str | None = None,
    internal_retention_until: datetime | None = None,
    subscription_terminated_at: datetime | None = None,
    subscription_tier: str | None = None,
) -> ParlayApiOutputRetentionEvidence:
    """Create capture-time evidence bound to the current product policy identity."""

    return ParlayApiOutputRetentionEvidence(
        output_class=output_class,
        source_payload_sha256=source_payload_sha256,
        acquisition_identity_sha256=acquisition_identity_sha256,
        captured_at=captured_at,
        available_at=available_at,
        policy_identity=PARLAYAPI_TERMS_POLICY_IDENTITY,
        observed_cache_control=observed_cache_control,
        internal_retention_until=internal_retention_until,
        subscription_terminated_at=subscription_terminated_at,
        subscription_tier=subscription_tier,
    )


@dataclass(frozen=True, slots=True)
class ParlayApiDeletionTombstone:
    """Non-Output audit metadata proving a raw capture was deleted."""

    source_payload_sha256: str
    acquisition_identity_sha256: str
    output_class: ParlayApiOutputClass
    captured_at: datetime
    deleted_at: datetime
    policy_identity: str
    reason: str

    def __post_init__(self) -> None:
        _require_sha256(self.source_payload_sha256, "source_payload_sha256")
        _require_sha256(self.acquisition_identity_sha256, "acquisition_identity_sha256")
        if not isinstance(self.output_class, ParlayApiOutputClass):
            raise ParlayApiRetentionError("output_class must be ParlayApiOutputClass")
        _require_aware(self.captured_at, "captured_at")
        _require_aware(self.deleted_at, "deleted_at")
        if self.deleted_at < self.captured_at:
            raise ParlayApiRetentionError("deleted_at cannot precede captured_at")
        _require_text(self.policy_identity, "policy_identity")
        _require_text(self.reason, "reason")


def deletion_tombstone(
    evidence: ParlayApiOutputRetentionEvidence,
    *,
    deleted_at: datetime,
    reason: str,
) -> ParlayApiDeletionTombstone:
    if not isinstance(evidence, ParlayApiOutputRetentionEvidence):
        raise TypeError("evidence must be ParlayApiOutputRetentionEvidence")
    _require_aware(deleted_at, "deleted_at")
    _require_text(reason, "reason")
    return ParlayApiDeletionTombstone(
        source_payload_sha256=evidence.source_payload_sha256,
        acquisition_identity_sha256=evidence.acquisition_identity_sha256,
        output_class=evidence.output_class,
        captured_at=evidence.captured_at,
        deleted_at=deleted_at,
        policy_identity=evidence.policy_identity,
        reason=reason,
    )


def _cache_control_deadline(
    cache_control: str | None,
    *,
    captured_at: datetime,
) -> datetime | None:
    if cache_control is None:
        return None
    directives = [part.strip() for part in cache_control.split(",") if part.strip()]
    deadlines: list[datetime] = []
    for directive in directives:
        name, separator, value = directive.partition("=")
        normalized = name.strip().lower()
        if normalized == "no-store":
            deadlines.append(captured_at)
        elif normalized in {"max-age", "s-maxage"}:
            if not separator:
                raise ParlayApiRetentionError(
                    f"{normalized} cache-control directive requires integer seconds"
                )
            raw = value.strip().strip('"')
            if not raw.isascii() or not raw.isdigit():
                raise ParlayApiRetentionError(
                    f"{normalized} cache-control directive requires non-negative integer seconds"
                )
            try:
                seconds = int(raw)
                deadline = captured_at + timedelta(seconds=seconds)
            except (OverflowError, ValueError) as exc:
                raise ParlayApiRetentionError(
                    f"{normalized} cache-control age exceeds supported datetime range"
                ) from exc
            deadlines.append(deadline)
    return min(deadlines) if deadlines else None


def _cache_control_requires_revalidation(cache_control: str | None) -> bool:
    if cache_control is None:
        return False
    return any(
        part.strip().partition("=")[0].strip().lower() == "no-cache"
        for part in cache_control.split(",")
        if part.strip()
    )


def _require_sha256(value: str, field: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ParlayApiRetentionError(f"{field} must be lowercase SHA-256 hex")


def _require_text(value: str, field: str) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise ParlayApiRetentionError(f"{field} must be non-empty trimmed text")


def _require_aware(value: datetime, field: str) -> None:
    # Retention timestamps participate directly in deadline arithmetic and ordering.
    # Reject both datetime subclasses and caller-defined tzinfo implementations:
    # either can override arithmetic/comparison/UTC-offset behavior and move a
    # binding deletion boundary. Built-in datetime.timezone is fixed-offset and
    # non-subclassable, so later comparisons cannot change its offset dynamically.
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ParlayApiRetentionError(
            f"{field} must be exact timezone-aware datetime with built-in fixed-offset timezone"
        )
