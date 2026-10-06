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
    PRODUCT_ORIGIN_UNPROVEN = "PRODUCT_ORIGIN_UNPROVEN"


@dataclass(frozen=True, slots=True)
class MarketActionabilityEvidence:
    """Caller-supplied structural evidence; never positive product-origin authority."""

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
    def is_product_issued(self) -> bool:
        """Standalone DTOs never carry product-issued positive authority."""
        return False

    @property
    def product_origin_proven(self) -> bool:
        """Canonical live/product origin is not proven by this additive contract."""
        return False

    @property
    def actionable(self) -> bool:
        # A public/caller-constructible result DTO must not mint positive authority.
        return (
            self.is_product_issued
            and self.product_origin_proven
            and self.action is ActionabilityAction.ACTIONABLE
        )


def _text_resolved(value: str | None) -> bool:
    # Exact strings only: subclasses can override strip/bool dispatch.
    return type(value) is str and bool(value and value.strip())


def _wait(
    reason: ActionabilityReason,
    evidence: MarketActionabilityEvidence,
) -> MarketActionabilityDecision:
    digest = (
        evidence.evidence_digest.strip()
        if type(evidence.evidence_digest) is str
        else ""
    )
    return MarketActionabilityDecision(
        action=ActionabilityAction.WAIT,
        reason=reason,
        evidence_digest=digest,
    )


def evaluate_market_actionability(
    evidence: MarketActionabilityEvidence,
) -> MarketActionabilityDecision:
    """Return ACTIONABLE only for coherent, fresh, OPEN canonical evidence.

    This function is intentionally fail-closed and side-effect free. It validates
    structural diagnostics only and never issues positive product-origin authority.
    A separate canonical product composition must re-resolve live evidence before
    downstream actionability may become positive.
    """

    if type(evidence) is not MarketActionabilityEvidence:
        raise TypeError("evidence must be an exact MarketActionabilityEvidence")
    if not _text_resolved(evidence.provider_id):
        return _wait(ActionabilityReason.PROVIDER_UNRESOLVED, evidence)
    if not _text_resolved(evidence.quote_id):
        return _wait(ActionabilityReason.QUOTE_UNRESOLVED, evidence)
    if not _text_resolved(evidence.evidence_digest):
        return _wait(ActionabilityReason.EVIDENCE_UNRESOLVED, evidence)
    if type(evidence.max_quote_age) is not timedelta:
        return _wait(ActionabilityReason.INVALID_MAX_QUOTE_AGE, evidence)
    if evidence.max_quote_age <= timedelta(0):
        return _wait(ActionabilityReason.INVALID_MAX_QUOTE_AGE, evidence)
    if (
        type(evidence.quote_observed_at) is not datetime
        or type(evidence.decision_at) is not datetime
    ):
        return _wait(ActionabilityReason.INVALID_TIMESTAMP, evidence)

    try:
        quote_tz = evidence.quote_observed_at.utcoffset()
        decision_tz = evidence.decision_at.utcoffset()
    except Exception:
        return _wait(ActionabilityReason.INVALID_TIMESTAMP, evidence)
    if quote_tz is None or decision_tz is None:
        return _wait(ActionabilityReason.INVALID_TIMESTAMP, evidence)

    try:
        age = evidence.decision_at - evidence.quote_observed_at
    except (TypeError, ValueError, OverflowError):
        return _wait(ActionabilityReason.INVALID_TIMESTAMP, evidence)
    if age < timedelta(0):
        return _wait(ActionabilityReason.QUOTE_AFTER_DECISION, evidence)
    if age >= evidence.max_quote_age:
        return _wait(ActionabilityReason.QUOTE_STALE, evidence)

    status = (
        evidence.market_status.strip().upper()
        if type(evidence.market_status) is str
        else ""
    )
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

    # Structural coherence is useful diagnostic evidence, but every field above is
    # caller-constructible. Positive live actionability must be issued only by a
    # canonical product composition that re-resolves product-owned market/provider
    # origin. Until that composition exists, standalone evaluation remains WAIT.
    return _wait(ActionabilityReason.PRODUCT_ORIGIN_UNPROVEN, evidence)
