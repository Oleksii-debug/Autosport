from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from autosport.market_actionability import (
    ActionabilityAction,
    ActionabilityReason,
    MarketActionabilityEvidence,
    evaluate_market_actionability,
)


@pytest.fixture
def coherent_evidence() -> MarketActionabilityEvidence:
    decision_at = datetime(2026, 9, 23, 1, 30, tzinfo=timezone.utc)
    return MarketActionabilityEvidence(
        provider_id="provider:canonical",
        quote_id="quote-123",
        quote_session_id="session-7",
        decision_session_id="session-7",
        quote_observed_at=decision_at - timedelta(seconds=2),
        decision_at=decision_at,
        max_quote_age=timedelta(seconds=5),
        market_status="OPEN",
        evidence_digest="sha256:canonical-market-evidence",
    )


def test_coherent_fresh_open_evidence_is_actionable(
    coherent_evidence: MarketActionabilityEvidence,
) -> None:
    result = evaluate_market_actionability(coherent_evidence)

    assert result.action is ActionabilityAction.ACTIONABLE
    assert result.actionable is True
    assert result.reason is ActionabilityReason.ACTIONABLE
    assert result.evidence_digest == coherent_evidence.evidence_digest


@pytest.mark.parametrize("provider_id", [None, "", "   "])
def test_unresolved_provider_fails_closed_to_wait(
    coherent_evidence: MarketActionabilityEvidence,
    provider_id: str | None,
) -> None:
    result = evaluate_market_actionability(
        replace(coherent_evidence, provider_id=provider_id)
    )

    assert result.action is ActionabilityAction.WAIT
    assert result.actionable is False
    assert result.reason is ActionabilityReason.PROVIDER_UNRESOLVED
    assert result.evidence_digest == coherent_evidence.evidence_digest


@pytest.mark.parametrize(
    ("market_status", "reason"),
    [
        ("CLOSED", ActionabilityReason.MARKET_CLOSED),
        ("SUSPENDED", ActionabilityReason.MARKET_SUSPENDED),
        ("UNKNOWN", ActionabilityReason.MARKET_NOT_OPEN),
        (None, ActionabilityReason.MARKET_NOT_OPEN),
    ],
)
def test_non_open_market_fails_closed_to_wait(
    coherent_evidence: MarketActionabilityEvidence,
    market_status: str | None,
    reason: ActionabilityReason,
) -> None:
    result = evaluate_market_actionability(
        replace(coherent_evidence, market_status=market_status)
    )

    assert result.action is ActionabilityAction.WAIT
    assert result.reason is reason


def test_stale_quote_fails_closed_and_preserves_evidence_digest(
    coherent_evidence: MarketActionabilityEvidence,
) -> None:
    stale = replace(
        coherent_evidence,
        quote_observed_at=coherent_evidence.decision_at - timedelta(seconds=6),
    )

    result = evaluate_market_actionability(stale)

    assert result.action is ActionabilityAction.WAIT
    assert result.reason is ActionabilityReason.QUOTE_STALE
    assert result.evidence_digest == coherent_evidence.evidence_digest


def test_quote_after_decision_fails_causal_ordering(
    coherent_evidence: MarketActionabilityEvidence,
) -> None:
    future_quote = replace(
        coherent_evidence,
        quote_observed_at=coherent_evidence.decision_at + timedelta(microseconds=1),
    )

    result = evaluate_market_actionability(future_quote)

    assert result.action is ActionabilityAction.WAIT
    assert result.reason is ActionabilityReason.QUOTE_AFTER_DECISION


@pytest.mark.parametrize(
    ("quote_session_id", "decision_session_id"),
    [(None, "session-7"), ("session-7", None), ("session-a", "session-b")],
)
def test_session_lineage_must_be_resolved_and_equal(
    coherent_evidence: MarketActionabilityEvidence,
    quote_session_id: str | None,
    decision_session_id: str | None,
) -> None:
    result = evaluate_market_actionability(
        replace(
            coherent_evidence,
            quote_session_id=quote_session_id,
            decision_session_id=decision_session_id,
        )
    )

    assert result.action is ActionabilityAction.WAIT
    assert result.reason is ActionabilityReason.SESSION_LINEAGE_MISMATCH


@pytest.mark.parametrize("evidence_digest", [None, "", "   "])
def test_missing_canonical_evidence_digest_fails_closed(
    coherent_evidence: MarketActionabilityEvidence,
    evidence_digest: str | None,
) -> None:
    result = evaluate_market_actionability(
        replace(coherent_evidence, evidence_digest=evidence_digest)
    )

    assert result.action is ActionabilityAction.WAIT
    assert result.reason is ActionabilityReason.EVIDENCE_UNRESOLVED
    assert result.evidence_digest == ""


def test_naive_timestamp_fails_closed(
    coherent_evidence: MarketActionabilityEvidence,
) -> None:
    naive = replace(
        coherent_evidence,
        quote_observed_at=coherent_evidence.quote_observed_at.replace(tzinfo=None),
    )

    result = evaluate_market_actionability(naive)

    assert result.action is ActionabilityAction.WAIT
    assert result.reason is ActionabilityReason.INVALID_TIMESTAMP


def test_negative_max_age_fails_closed(
    coherent_evidence: MarketActionabilityEvidence,
) -> None:
    result = evaluate_market_actionability(
        replace(coherent_evidence, max_quote_age=timedelta(microseconds=-1))
    )

    assert result.action is ActionabilityAction.WAIT
    assert result.reason is ActionabilityReason.INVALID_MAX_QUOTE_AGE
