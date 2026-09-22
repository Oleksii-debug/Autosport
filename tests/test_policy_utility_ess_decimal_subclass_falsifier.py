from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.policy_utility_evidence import (
    DecisionKind,
    PolicyUtilityError,
    PolicyUtilityEvidence,
    UtilityCompleteness,
    UtilityTruthClass,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
NOW = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)


class _AdversarialEffectiveSampleSize(Decimal):
    def __new__(cls, value: str) -> "_AdversarialEffectiveSampleSize":
        return super().__new__(cls, value)

    def is_finite(self) -> bool:
        return True

    def __le__(self, other: object) -> bool:
        return False

    def __gt__(self, other: object) -> bool:
        return False


def _estimated_evidence(
    effective_sample_size: Decimal,
) -> PolicyUtilityEvidence:
    return PolicyUtilityEvidence(
        environment_id="env-ess-subclass",
        episode_id="episode-ess-subclass",
        action_id="action-ess-subclass",
        outcome_id="outcome-ess-subclass",
        reward_id="reward-ess-subclass",
        transition_id="transition-ess-subclass",
        policy_id="policy-ess-subclass",
        model_id="model-ess-subclass",
        strategy_id="strategy-ess-subclass",
        config_sha256=SHA_A,
        protocol_sha256=SHA_B,
        economic_goal_fingerprint=SHA_C,
        risk_fingerprint=SHA_D,
        bankroll_id="bankroll-ess-subclass",
        portfolio_identity="portfolio-ess-subclass",
        utility_definition_family="owner-net-utility",
        utility_definition_version="v1",
        utility_definition_sha256=SHA_E,
        completeness=UtilityCompleteness.INCOMPLETE,
        truth_class=UtilityTruthClass.ESTIMATED,
        decision_kind=DecisionKind.POSITIONED,
        available_at=NOW,
        support_count=2,
        effective_sample_size=effective_sample_size,
        uncertainty=Decimal("0"),
    )


def test_decimal_subclass_cannot_bypass_ess_raw_support_bound() -> None:
    boundary = _estimated_evidence(Decimal("2"))
    assert boundary.effective_sample_size == Decimal("2")

    hostile = _AdversarialEffectiveSampleSize("1000")
    assert hostile > Decimal("2") is False

    with pytest.raises(
        PolicyUtilityError,
        match="effective_sample_size",
    ):
        _estimated_evidence(hostile)
