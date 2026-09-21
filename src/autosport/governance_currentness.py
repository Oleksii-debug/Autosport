"""Decision-time qualification for historical bookmaker governance evidence.

This module does not grant execution authority. It only classifies whether durable
BookmakerGovernanceEvidence remains usable as bounded decision-time evidence under
an explicit freshness policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import math

from .bookmaker_capability_registry import (
    BookmakerCapabilityRegistry,
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)


class GovernanceCurrentnessError(ValueError):
    """Raised when currentness inputs are malformed or authority is substituted."""


class GovernanceEvidenceState(str, Enum):
    """Bounded evidence interpretation; never execution permission."""

    UNKNOWN = "unknown"
    SUPPORTS_PERMITTED = "supports_permitted"
    SUPPORTS_PROHIBITED = "supports_prohibited"


class GovernanceCurrentnessReason(str, Enum):
    NO_ELIGIBLE_EVIDENCE = "no_eligible_evidence"
    STALE = "stale"
    AMBIGUOUS_LATEST = "ambiguous_latest"
    TERMS_DOCUMENT_CONFLICT = "terms_document_conflict"
    RECORDED_UNKNOWN = "recorded_unknown"
    QUALIFIED_PERMITTED = "qualified_permitted"
    QUALIFIED_PROHIBITED = "qualified_prohibited"


def _timestamp(value: str, field: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise GovernanceCurrentnessError(f"{field} must be a non-empty trimmed string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GovernanceCurrentnessError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GovernanceCurrentnessError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _text(value: str, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise GovernanceCurrentnessError(f"{field} must be a non-empty trimmed string")
    return value


@dataclass(frozen=True, slots=True)
class GovernanceDecisionEvidence:
    venue_id: str
    account_id: str
    jurisdiction: str
    decision_at: str
    max_age_seconds: int
    state: GovernanceEvidenceState
    reason: GovernanceCurrentnessReason
    evidence_id: str | None
    terms_version: str | None
    source_payload_sha256: str | None
    evidence_age_seconds: float | None
    execution_authorized: bool = False

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.jurisdiction, "jurisdiction")
        _timestamp(self.decision_at, "decision_at")
        if type(self.max_age_seconds) is not int or self.max_age_seconds <= 0:
            raise GovernanceCurrentnessError(
                "max_age_seconds must be a positive integer"
            )
        if not isinstance(self.state, GovernanceEvidenceState):
            raise GovernanceCurrentnessError(
                "state must be a GovernanceEvidenceState"
            )
        if not isinstance(self.reason, GovernanceCurrentnessReason):
            raise GovernanceCurrentnessError(
                "reason must be a GovernanceCurrentnessReason"
            )
        if self.evidence_id is not None:
            if (
                type(self.evidence_id) is not str
                or len(self.evidence_id) != 64
                or any(char not in "0123456789abcdef" for char in self.evidence_id)
            ):
                raise GovernanceCurrentnessError(
                    "evidence_id must be a canonical SHA-256 digest"
                )
        if self.terms_version is not None:
            _text(self.terms_version, "terms_version")
        if self.source_payload_sha256 is not None:
            if (
                type(self.source_payload_sha256) is not str
                or len(self.source_payload_sha256) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in self.source_payload_sha256
                )
            ):
                raise GovernanceCurrentnessError(
                    "source_payload_sha256 must be a canonical SHA-256 digest"
                )
        if self.evidence_age_seconds is not None:
            if (
                type(self.evidence_age_seconds) is not float
                or not math.isfinite(self.evidence_age_seconds)
                or self.evidence_age_seconds < 0.0
            ):
                raise GovernanceCurrentnessError(
                    "evidence_age_seconds must be a finite non-negative float"
                )
        if self.execution_authorized is not False:
            raise GovernanceCurrentnessError(
                "governance currentness evidence cannot authorize execution"
            )


def _result(
    *,
    venue_id: str,
    account_id: str,
    jurisdiction: str,
    decision_at: str,
    max_age_seconds: int,
    state: GovernanceEvidenceState,
    reason: GovernanceCurrentnessReason,
    evidence: BookmakerGovernanceEvidence | None = None,
    evidence_age_seconds: float | None = None,
) -> GovernanceDecisionEvidence:
    return GovernanceDecisionEvidence(
        venue_id=venue_id,
        account_id=account_id,
        jurisdiction=jurisdiction,
        decision_at=decision_at,
        max_age_seconds=max_age_seconds,
        state=state,
        reason=reason,
        evidence_id=evidence.evidence_id if evidence is not None else None,
        terms_version=evidence.terms_version if evidence is not None else None,
        source_payload_sha256=(
            evidence.source_payload_sha256 if evidence is not None else None
        ),
        evidence_age_seconds=evidence_age_seconds,
        execution_authorized=False,
    )


def resolve_governance_for_decision(
    registry: BookmakerCapabilityRegistry,
    *,
    venue_id: str,
    account_id: str,
    jurisdiction: str,
    decision_at: str,
    max_age_seconds: int,
) -> GovernanceDecisionEvidence:
    """Qualify durable governance evidence at one decision cutoff.

    Positive qualification means only that the newest unambiguous durable evidence
    within the caller's explicit freshness policy records PERMITTED or PROHIBITED.
    It never grants financial/execution authority and it never infers legal permission
    from technical capability.
    """

    if type(registry) is not BookmakerCapabilityRegistry:
        raise GovernanceCurrentnessError(
            "registry must be the exact durable BookmakerCapabilityRegistry"
        )
    _text(venue_id, "venue_id")
    _text(account_id, "account_id")
    _text(jurisdiction, "jurisdiction")
    decision_time = _timestamp(decision_at, "decision_at")
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise GovernanceCurrentnessError(
            "max_age_seconds must be a positive integer"
        )

    history = registry.governance_history(venue_id, account_id)
    if any(type(item) is not BookmakerGovernanceEvidence for item in history):
        raise GovernanceCurrentnessError(
            "registry returned non-canonical governance evidence"
        )

    scoped = tuple(item for item in history if item.jurisdiction == jurisdiction)
    eligible = tuple(
        item
        for item in scoped
        if _timestamp(item.observed_at, "observed_at") <= decision_time
    )
    if not eligible:
        return _result(
            venue_id=venue_id,
            account_id=account_id,
            jurisdiction=jurisdiction,
            decision_at=decision_at,
            max_age_seconds=max_age_seconds,
            state=GovernanceEvidenceState.UNKNOWN,
            reason=GovernanceCurrentnessReason.NO_ELIGIBLE_EVIDENCE,
        )

    latest_time = max(_timestamp(item.observed_at, "observed_at") for item in eligible)
    latest = tuple(
        item
        for item in eligible
        if _timestamp(item.observed_at, "observed_at") == latest_time
    )
    if len(latest) != 1:
        return _result(
            venue_id=venue_id,
            account_id=account_id,
            jurisdiction=jurisdiction,
            decision_at=decision_at,
            max_age_seconds=max_age_seconds,
            state=GovernanceEvidenceState.UNKNOWN,
            reason=GovernanceCurrentnessReason.AMBIGUOUS_LATEST,
        )
    candidate = latest[0]
    age_seconds = float((decision_time - latest_time).total_seconds())

    same_terms_digests = {
        item.source_payload_sha256
        for item in eligible
        if item.terms_version == candidate.terms_version
    }
    if len(same_terms_digests) != 1:
        return _result(
            venue_id=venue_id,
            account_id=account_id,
            jurisdiction=jurisdiction,
            decision_at=decision_at,
            max_age_seconds=max_age_seconds,
            state=GovernanceEvidenceState.UNKNOWN,
            reason=GovernanceCurrentnessReason.TERMS_DOCUMENT_CONFLICT,
            evidence=candidate,
            evidence_age_seconds=age_seconds,
        )

    if age_seconds > max_age_seconds:
        return _result(
            venue_id=venue_id,
            account_id=account_id,
            jurisdiction=jurisdiction,
            decision_at=decision_at,
            max_age_seconds=max_age_seconds,
            state=GovernanceEvidenceState.UNKNOWN,
            reason=GovernanceCurrentnessReason.STALE,
            evidence=candidate,
            evidence_age_seconds=age_seconds,
        )

    if candidate.automation_permission is GovernancePermissionState.UNKNOWN:
        state = GovernanceEvidenceState.UNKNOWN
        reason = GovernanceCurrentnessReason.RECORDED_UNKNOWN
    elif candidate.automation_permission is GovernancePermissionState.PERMITTED:
        state = GovernanceEvidenceState.SUPPORTS_PERMITTED
        reason = GovernanceCurrentnessReason.QUALIFIED_PERMITTED
    elif candidate.automation_permission is GovernancePermissionState.PROHIBITED:
        state = GovernanceEvidenceState.SUPPORTS_PROHIBITED
        reason = GovernanceCurrentnessReason.QUALIFIED_PROHIBITED
    else:
        raise GovernanceCurrentnessError(
            "unsupported governance permission state"
        )

    return _result(
        venue_id=venue_id,
        account_id=account_id,
        jurisdiction=jurisdiction,
        decision_at=decision_at,
        max_age_seconds=max_age_seconds,
        state=state,
        reason=reason,
        evidence=candidate,
        evidence_age_seconds=age_seconds,
    )
