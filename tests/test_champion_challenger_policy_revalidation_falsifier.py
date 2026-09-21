import math

import pytest

from autosport.champion_challenger_gate import (
    PairedLoss,
    PromotionEvidenceError,
    PromotionPolicy,
    evaluate_promotion,
)


def _constructor_bypassed_policy(**overrides: object) -> PromotionPolicy:
    values: dict[str, object] = {
        "min_pairs": 1,
        "min_effective_pairs": 1,
        "min_mean_improvement": 0.0,
        "min_win_rate": 0.5,
        "alpha": 0.05,
        "tie_tolerance": 0.0,
    }
    values.update(overrides)
    policy = object.__new__(PromotionPolicy)
    for name, value in values.items():
        object.__setattr__(policy, name, value)
    return policy


def test_evaluator_revalidates_policy_before_positive_promotion() -> None:
    forged = _constructor_bypassed_policy(
        min_pairs=0,
        min_effective_pairs=0,
        min_mean_improvement=math.nan,
        min_win_rate=math.nan,
        alpha=math.nan,
    )

    with pytest.raises(PromotionEvidenceError):
        evaluate_promotion([PairedLoss("eval-1", 1.0, 0.0)], forged)


def test_evaluator_revalidates_finite_policy_thresholds() -> None:
    forged = _constructor_bypassed_policy(alpha=float("inf"))

    with pytest.raises(PromotionEvidenceError):
        evaluate_promotion([PairedLoss("eval-1", 1.0, 0.0)], forged)
