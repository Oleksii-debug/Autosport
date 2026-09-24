from types import SimpleNamespace

from autosport.scientific_registry import (
    PromotionAction,
    PromotionEvidenceDirection,
    PromotionEvidenceValidity,
)
from autosport.strategy_model_factory import (
    PromotionController,
    PromotionRule,
    PromotionVerdict,
)


def _eligible_evidence():
    return SimpleNamespace(
        validity=PromotionEvidenceValidity.ELIGIBLE,
        holdout_consumed=False,
        minimum_effective_sample_size=2,
        effective_sample_size=2,
        guardrails_passed=True,
        estimand="mse",
        direction=PromotionEvidenceDirection.LOWER_IS_BETTER,
        rollback_identity="strategy-v1",
        practical_improvement="0.15",
        effect_interval_low="0.10",
    )


def test_zero_threshold_observed_tie_cannot_promote_with_positive_evidence():
    result = PromotionController.evaluate(
        PromotionRule("mse", 0.0),
        champion_metrics={"mse": 0.20},
        challenger_metrics={"mse": 0.20},
        provenance_complete=True,
        rollback_target="strategy-v1",
        promotion_evidence=_eligible_evidence(),
    )

    assert result.verdict is PromotionVerdict.REJECT
    assert result.registry_action is PromotionAction.REJECT
    assert result.primary_improvement == 0.0
    assert result.reasons == ("primary improvement must be strictly positive",)


def test_zero_threshold_positive_observed_improvement_preserves_promotion_path():
    result = PromotionController.evaluate(
        PromotionRule("mse", 0.0),
        champion_metrics={"mse": 0.40},
        challenger_metrics={"mse": 0.20},
        provenance_complete=True,
        rollback_target="strategy-v1",
        promotion_evidence=_eligible_evidence(),
    )

    assert result.verdict is PromotionVerdict.PROMOTE
    assert result.registry_action is PromotionAction.PROMOTE
    assert result.primary_improvement == 0.20
