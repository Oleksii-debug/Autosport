from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.forward_economic_evidence import (
    BetSide,
    ForwardEconomicEvidenceError,
    ResolvedPolicyOutcome,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
EVENT_SHA = "1" * 64
DECISION_SHA = "2" * 64
EXECUTION_SHA = "3" * 64
SETTLEMENT_SHA = "4" * 64


def _executed_outcome(
    *,
    decision_committed_at: datetime,
    execution_accepted_at: datetime,
) -> ResolvedPolicyOutcome:
    return ResolvedPolicyOutcome(
        policy_id="challenger",
        sequence=0,
        universe_event_sha256=EVENT_SHA,
        decision_sha256=DECISION_SHA,
        decision_committed_at=decision_committed_at,
        side=BetSide.BACK,
        accepted_odds=Decimal("2"),
        accepted_stake=Decimal("10"),
        net_pnl_currency=Decimal("5"),
        execution_evidence_sha256=EXECUTION_SHA,
        execution_accepted_at=execution_accepted_at,
        settlement_evidence_sha256=SETTLEMENT_SHA,
        settlement_available_at=execution_accepted_at + timedelta(minutes=1),
    )


@pytest.mark.parametrize(
    "execution_accepted_at",
    (
        T0,
        T0.astimezone(timezone(timedelta(hours=1))),
    ),
)
def test_execution_acceptance_must_strictly_follow_committed_decision(
    execution_accepted_at: datetime,
) -> None:
    """Equal authoritative instants do not prove decision-before-execution."""

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="execution acceptance.*committed decision",
    ):
        _executed_outcome(
            decision_committed_at=T0,
            execution_accepted_at=execution_accepted_at,
        )


def test_strictly_later_execution_acceptance_remains_valid() -> None:
    outcome = _executed_outcome(
        decision_committed_at=T0,
        execution_accepted_at=T0 + timedelta(microseconds=1),
    )

    assert outcome.decision_committed_at == T0
    assert outcome.execution_accepted_at == T0 + timedelta(microseconds=1)
