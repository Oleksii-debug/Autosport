from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.economic_goal import EconomicGoalContract
from autosport.paper_risk_policy_store import (
    PaperRiskPolicyStoreError,
    paper_risk_policy_from_payload,
    paper_risk_policy_to_payload,
)
from autosport.risk import PaperRiskPolicy


class _CallerControlledGoal(EconomicGoalContract):
    """Prove an isinstance-compatible subclass can change authority reads."""

    def __getattribute__(self, name: str) -> object:
        if name == "goal_id":
            return "caller-substituted-goal"
        return super().__getattribute__(name)


def _goal(goal_type: type[EconomicGoalContract] = EconomicGoalContract) -> EconomicGoalContract:
    return goal_type(
        goal_id="owner-paper-campaign",
        revision=1,
        bankroll_id="paper-main",
        currency="EUR",
    )


def _policy(goal: EconomicGoalContract) -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.017"),
        max_committed_fraction=Decimal("0.133"),
        minimum_cash_reserve_fraction=Decimal("0.271"),
        economic_goal=goal,
    )


def test_serialization_rejects_isinstance_compatible_goal_subclass() -> None:
    spoofed_goal = _goal(_CallerControlledGoal)
    assert isinstance(spoofed_goal, EconomicGoalContract)
    assert spoofed_goal.goal_id == "caller-substituted-goal"

    policy = _policy(spoofed_goal)

    with pytest.raises(
        PaperRiskPolicyStoreError,
        match="canonical EconomicGoalContract",
    ):
        paper_risk_policy_to_payload(policy)


def test_reconstruction_rejects_isinstance_compatible_goal_subclass() -> None:
    canonical_goal = _goal()
    canonical_policy = _policy(canonical_goal)
    payload = paper_risk_policy_to_payload(canonical_policy)
    spoofed_goal = _goal(_CallerControlledGoal)

    with pytest.raises(
        PaperRiskPolicyStoreError,
        match="canonical EconomicGoalContract",
    ):
        paper_risk_policy_from_payload(
            payload,
            economic_goal=spoofed_goal,
            expected_policy_provenance_sha256=canonical_policy.provenance_sha256,
        )
