import math

import pytest

from autosport.scientific_registry import PromotionAction
from autosport.strategy_model_factory import (
    DriftEvidence,
    DriftMonitor,
    MeanBaselineModel,
    PromotionController,
    PromotionRule,
    PromotionVerdict,
    TrainingPoint,
    WalkForwardRunner,
)


def _points():
    return (
        TrainingPoint("2026-01-01T00:00:00+00:00", 1.0, 0.0),
        TrainingPoint("2026-01-02T00:00:00+00:00", 2.0, 1.0),
        TrainingPoint("2026-01-03T00:00:00+00:00", 3.0, 1.0),
        TrainingPoint("2026-01-04T00:00:00+00:00", 4.0, 0.0),
    )


def test_mean_baseline_uses_only_observations_at_or_before_training_cutoff():
    model = MeanBaselineModel.fit(
        "baseline-1", _points(), training_cutoff="2026-01-02T00:00:00+00:00"
    )
    assert model.training_count == 2
    assert model.mean_target == 0.5
    assert len(model.identity_sha256) == 64


def test_mean_baseline_rejects_future_prediction_input():
    model = MeanBaselineModel.fit(
        "baseline-1", _points()[:2], training_cutoff="2026-01-02T00:00:00+00:00"
    )
    with pytest.raises(ValueError, match="not available"):
        model.predict(
            TrainingPoint("2026-01-04T00:00:00+00:00", 4.0, 0.0),
            decision_at="2026-01-03T00:00:00+00:00",
        )


def test_walk_forward_is_expanding_window_and_deterministic():
    first = WalkForwardRunner.run(_points(), minimum_train_size=2)
    second = WalkForwardRunner.run(tuple(reversed(_points())), minimum_train_size=2)
    assert first == second
    assert first.result_sha256 == second.result_sha256
    assert [fold.training_cutoff for fold in first.folds] == [
        "2026-01-02T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
    ]
    assert all(fold.training_cutoff < fold.evaluation_at for fold in first.folds)
    assert first.primary_metric == "mse"


def test_walk_forward_rejects_duplicate_timestamp_identity():
    points = _points()[:2] + (
        TrainingPoint("2026-01-02T00:00:00+00:00", 9.0, 0.0),
    )
    with pytest.raises(ValueError, match="unique timestamps"):
        WalkForwardRunner.run(points, minimum_train_size=2)


def test_promotion_requires_provenance_rollback_primary_and_protective_metrics():
    rule = PromotionRule("mse", 0.05, (("max_drawdown", 0.20),))
    accepted = PromotionController.evaluate(
        rule,
        champion_metrics={"mse": 0.40, "max_drawdown": 0.10},
        challenger_metrics={"mse": 0.30, "max_drawdown": 0.15},
        provenance_complete=True,
        rollback_target="strategy-v1",
    )
    assert accepted.verdict is PromotionVerdict.PROMOTE
    assert accepted.registry_action is PromotionAction.PROMOTE

    degraded = PromotionController.evaluate(
        rule,
        champion_metrics={"mse": 0.40, "max_drawdown": 0.10},
        challenger_metrics={"mse": 0.20, "max_drawdown": 0.25},
        provenance_complete=True,
        rollback_target="strategy-v1",
    )
    assert degraded.verdict is PromotionVerdict.REJECT
    assert degraded.registry_action is PromotionAction.REJECT
    assert "protective metric degraded: max_drawdown" in degraded.reasons

    incomplete = PromotionController.evaluate(
        rule,
        champion_metrics={"mse": 0.40},
        challenger_metrics={"mse": 0.20},
        provenance_complete=False,
        rollback_target="strategy-v1",
    )
    assert incomplete.verdict is PromotionVerdict.REJECT


def test_factory_rejects_nonfinite_metrics():
    rule = PromotionRule("mse", 0.0)
    with pytest.raises(ValueError, match="finite"):
        PromotionController.evaluate(
            rule,
            champion_metrics={"mse": 1.0},
            challenger_metrics={"mse": math.nan},
            provenance_complete=True,
            rollback_target="strategy-v1",
        )


def test_drift_monitor_only_emits_research_recommendations():
    recommendations = DriftMonitor.recommendations(
        (
            DriftEvidence("mse", 0.20, 0.21, 0.05, "2026-01-05T00:00:00+00:00"),
            DriftEvidence("calibration", 0.02, 0.20, 0.05, "2026-01-05T00:00:00+00:00"),
        )
    )
    assert recommendations == (
        "RESEARCH_CHALLENGER:calibration:2026-01-05T00:00:00+00:00",
    )
    assert all("PROMOTE" not in item for item in recommendations)
