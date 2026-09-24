"""Fail-closed market-evidence actionability contract.

This module deliberately does not authorize betting or execution. It answers the
smaller question of whether canonical market evidence is coherent enough for a
downstream decision stage to continue. Any uncertainty returns WAIT.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class ActionabilityAction(str, Enum):
    """Downstream routing outcome for market evidence."""

    ACTIONABLE = "ACTIONABLE"
    WAIT = "WAIT"


class ActionabilityReason(str, Enum):
    """Deterministic reason codes for actionability decisions."""

    ACTIONABLE = "ACTIONABLE"
    PROVIDER_UNRESOLVED = "PROVIDER_UNRESOLVED"
    QUOTE_UNRESOLVED = "QUOTE_UNRESOLVED"
    EVIDENCE_UNRESOLVED = "EVIDENCE_UNRESOLVED"
    INVALID_MAX_QUOTE_AGE = "INVALID_MAX_QUOTE_AGE"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    QUOTE_AFTER_DECISION = "QUOTE_AFTER_DECISION"
    QUOTE_STALE = "QUOTE_STALE"
    MARKET_CLOSED = "MARKET_CLOSED"
    MARKET_SUSPENDED = "MARKET_SUSPENDED"
    MARKET_NOT_OPEN = "MARKET_NOT_OPEN"
    SESSION_LINEAGE_MISMATCH = "SESSION_LINEAGE_MISMATCH"


@dataclass(frozen=True, slots=True)
class MarketActionabilityEvidence:
    """Product-owned evidence required to assess market actionability."""

    provider_id: str | None
    quote_id: str | None
    quote_session_id: str | None
    decision_session_id: str | None
    quote_observed_at: datetime
    decision_at: datetime
    max_quote_age: timedelta
    market_status: str | None
    evidence_digest: str | None


@dataclass(frozen=True, slots=True)
class MarketActionabilityDecision:
    """Fail-closed result carrying the exact canonical evidence digest."""

    action: ActionabilityAction
    reason: ActionabilityReason
    evidence_digest: str

    @property
    def actionable(self) -> bool:
        return self.action is ActionabilityAction.ACTIONABLE


def _text_resolved(value: str | None) -> bool:
    return bool(value and value.strip())


def _wait(
    reason: ActionabilityReason,
    evidence: MarketActionabilityEvidence,
) -> MarketActionabilityDecision:
    return MarketActionabilityDecision(
        action=ActionabilityAction.WAIT,
        reason=reason,
        evidence_digest=(evidence.evidence_digest or "").strip(),
    )


def evaluate_market_actionability(
    evidence: MarketActionabilityEvidence,
) -> MarketActionabilityDecision:
    """Return ACTIONABLE only for coherent, fresh, OPEN canonical evidence.

    This function is intentionally fail-closed and side-effect free. ACTIONABLE
    means only that a downstream decision stage may evaluate the market; it is
    not execution permission.
    """

    if not _text_resolved(evidence.provider_id):
        return _wait(ActionabilityReason.PROVIDER_UNRESOLVED, evidence)
    if not _text_resolved(evidence.quote_id):
        return _wait(ActionabilityReason.QUOTE_UNRESOLVED, evidence)
    if not _text_resolved(evidence.evidence_digest):
        return _wait(ActionabilityReason.EVIDENCE_UNRESOLVED, evidence)
    if evidence.max_quote_age < timedelta(0):
        return _wait(ActionabilityReason.INVALID_MAX_QUOTE_AGE, evidence)

    quote_tz = evidence.quote_observed_at.utcoffset()
    decision_tz = evidence.decision_at.utcoffset()
    if quote_tz is None or decision_tz is None:
        return _wait(ActionabilityReason.INVALID_TIMESTAMP, evidence)

    age = evidence.decision_at - evidence.quote_observed_at
    if age < timedelta(0):
        return _wait(ActionabilityReason.QUOTE_AFTER_DECISION, evidence)
    if age > evidence.max_quote_age:
        return _wait(ActionabilityReason.QUOTE_STALE, evidence)

    status = (evidence.market_status or "").strip().upper()
    if status == "CLOSED":
        return _wait(ActionabilityReason.MARKET_CLOSED, evidence)
    if status == "SUSPENDED":
        return _wait(ActionabilityReason.MARKET_SUSPENDED, evidence)
    if status != "OPEN":
        return _wait(ActionabilityReason.MARKET_NOT_OPEN, evidence)

    if (
        not _text_resolved(evidence.quote_session_id)
        or not _text_resolved(evidence.decision_session_id)
        or evidence.quote_session_id != evidence.decision_session_id
    ):
        return _wait(ActionabilityReason.SESSION_LINEAGE_MISMATCH, evidence)

    return MarketActionabilityDecision(
        action=ActionabilityAction.ACTIONABLE,
        reason=ActionabilityReason.ACTIONABLE,
        evidence_digest=evidence.evidence_digest.strip(),
    )
