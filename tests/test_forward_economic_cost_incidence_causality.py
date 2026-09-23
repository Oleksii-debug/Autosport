from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.forward_economic_evidence import (
    BetSide,
    ForwardEconomicEvidenceError,
    ResolvedPolicyOutcome,
)


T0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
EVENT_SHA = "a" * 64
DECISION_SHA = "b" * 64
COST_EVIDENCE_SHA = "c" * 64


def _cost_only_outcome(*, incurred_at: datetime) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=0,
        universe_event_sha256=EVENT_SHA,
        decision_sha256=DECISION_SHA,
        decision_committed_at=T0,
        side=BetSide.NONE,
        accepted_odds=None,
        accepted_stake=None,
        net_pnl_currency=Decimal("-2"),
        execution_evidence_sha256=None,
        execution_accepted_at=None,
        settlement_evidence_sha256=None,
        settlement_available_at=None,
        wager_pnl_currency=Decimal("0"),
        economic_cost_currency=Decimal("2"),
        economic_cost_evidence_sha256=COST_EVIDENCE_SHA,
        economic_cost_incurred_at=incurred_at,
        economic_cost_available_at=T0 + timedelta(minutes=1),
    )


def test_economic_cost_incidence_cannot_precede_committed_decision() -> None:
    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="economic cost cannot be incurred before the committed decision",
    ):
        _cost_only_outcome(incurred_at=T0 - timedelta(microseconds=1))


def test_economic_cost_incidence_at_committed_decision_remains_admissible() -> None:
    outcome = _cost_only_outcome(incurred_at=T0)

    assert outcome.economic_cost_incurred_at == T0
    assert outcome.economic_cost_available_at == T0 + timedelta(minutes=1)
    assert outcome.effective_wager_pnl_currency == Decimal("0")
    assert outcome.net_pnl_currency == Decimal("-2")
