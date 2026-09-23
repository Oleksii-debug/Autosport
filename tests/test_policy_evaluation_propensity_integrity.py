from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.learning_environment import EvidenceTruth
from autosport.policy_evaluation import PolicyEvaluationCase, PolicyRewardMode


T0 = "2026-09-23T00:00:00Z"
T1 = "2026-09-23T00:01:00Z"
EVIDENCE = "a" * 64


def _case(
    propensities: tuple[tuple[str, Decimal], ...],
) -> PolicyEvaluationCase:
    return PolicyEvaluationCase(
        sample_id="propensity-integrity-case",
        observed_at=T0,
        reward_available_at=T1,
        admissible_actions=("BET", "WAIT"),
        action_rewards=(
            ("BET", Decimal("1")),
            ("WAIT", Decimal("0")),
        ),
        action_costs=(
            ("BET", Decimal("0.10")),
            ("WAIT", Decimal("0")),
        ),
        behavior_propensities=propensities,
        reward_truth=EvidenceTruth.OBSERVED,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        source_evidence_sha256=EVIDENCE,
        regime_id="table-tennis:pre-match",
        counterfactual_source_id="paper-settlement-engine:v1",
    )


@pytest.mark.parametrize(
    "propensities",
    (
        (
            ("BET", Decimal("0.75")),
            ("WAIT", Decimal("0.75")),
        ),
        (
            ("BET", Decimal("0.25")),
            ("WAIT", Decimal("0.25")),
        ),
    ),
)
def test_behavior_propensity_probability_mass_must_equal_one(
    propensities: tuple[tuple[str, Decimal], ...],
) -> None:
    """Logging-policy propensities cannot be caller-scaled weights.

    The evaluator later uses 1 / propensity to derive importance weights and
    effective sample size. Accepting a positive-support vector whose total
    probability mass is not exactly one lets a caller alter those diagnostics
    without changing the frozen action/reward population.
    """

    with pytest.raises(ValueError, match="propensit|probability|mass|sum"):
        _case(propensities)


def test_normalized_behavior_propensity_probability_mass_remains_valid() -> None:
    case = _case(
        (
            ("BET", Decimal("0.50")),
            ("WAIT", Decimal("0.50")),
        )
    )

    assert sum(
        (value for _action, value in case.behavior_propensities),
        Decimal("0"),
    ) == Decimal("1")
