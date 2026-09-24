from __future__ import annotations

from decimal import Context, Decimal, ROUND_DOWN, ROUND_UP, localcontext

import pytest

from autosport.binary_probability_baseline import (
    BinaryOutcomeMeanProbabilityFactory,
    BinaryOutcomeMeanProbabilityModel,
    BinaryProbabilityModelError,
    MODEL_FAMILY,
    PREDICTION_SEMANTICS,
    TARGET_SEMANTICS,
)
from autosport.strategy_model_factory import (
    TrainingPoint,
    WalkForwardRunner,
    training_points_manifest_sha256,
)


def _point(
    observed_at: str,
    target: float,
    *,
    revealed_at: str,
    feature: float = 0.5,
    evidence_digit: str = "a",
) -> TrainingPoint:
    return TrainingPoint(
        observed_at=observed_at,
        feature=feature,
        target=target,
        target_available_at=revealed_at,
        evidence_sha256s=(evidence_digit * 64,),
    )


def test_probability_factory_rejects_non_binary_target_domain() -> None:
    points = (
        _point(
            "2026-01-01T00:00:00Z",
            1.0,
            revealed_at="2026-01-01T01:00:00Z",
            evidence_digit="a",
        ),
        _point(
            "2026-01-02T00:00:00Z",
            0.25,
            revealed_at="2026-01-02T01:00:00Z",
            evidence_digit="b",
        ),
    )

    with pytest.raises(BinaryProbabilityModelError, match="Bernoulli outcome"):
        BinaryOutcomeMeanProbabilityFactory().fit(
            "model-v1",
            points,
            training_cutoff="2026-01-03T00:00:00Z",
        )


def test_probability_factory_rejects_future_non_binary_target_too() -> None:
    points = (
        _point(
            "2026-01-01T00:00:00Z",
            1.0,
            revealed_at="2026-01-01T01:00:00Z",
            evidence_digit="a",
        ),
        _point(
            "2026-02-01T00:00:00Z",
            0.5,
            revealed_at="2026-02-02T00:00:00Z",
            evidence_digit="b",
        ),
    )

    with pytest.raises(BinaryProbabilityModelError, match="Bernoulli outcome"):
        BinaryOutcomeMeanProbabilityFactory().fit(
            "model-v1",
            points,
            training_cutoff="2026-01-03T00:00:00Z",
        )


def test_probability_model_binds_only_causally_revealed_training_subset() -> None:
    first = _point(
        "2026-01-01T00:00:00Z",
        1.0,
        revealed_at="2026-01-01T12:00:00Z",
        evidence_digit="a",
    )
    not_yet_revealed = _point(
        "2026-01-02T00:00:00Z",
        0.0,
        revealed_at="2026-01-03T12:00:00Z",
        evidence_digit="b",
    )

    model = BinaryOutcomeMeanProbabilityFactory().fit(
        "model-v1",
        (not_yet_revealed, first),
        training_cutoff="2026-01-02T12:00:00Z",
    )

    assert model.success_count == 1
    assert model.training_count == 1
    assert model.probability == Decimal("1")
    assert model.causal_training_manifest_sha256 == training_points_manifest_sha256(
        (first,)
    )


def test_probability_arithmetic_ignores_ambient_decimal_context() -> None:
    model = BinaryOutcomeMeanProbabilityModel(
        model_id="model-v1",
        training_cutoff="2026-01-01T00:00:00Z",
        success_count=1,
        training_count=3,
        causal_training_manifest_sha256="c" * 64,
    )

    with localcontext(Context(prec=3, rounding=ROUND_DOWN)):
        low_precision = model.probability
    with localcontext(Context(prec=80, rounding=ROUND_UP)):
        high_precision = model.probability

    assert low_precision == high_precision
    assert str(low_precision) == "0.3333333333333333333333333333333333"


def test_probability_artifact_round_trip_binds_semantics_and_identity() -> None:
    model = BinaryOutcomeMeanProbabilityModel(
        model_id="model-v1",
        training_cutoff="2026-01-01T00:00:00Z",
        success_count=2,
        training_count=3,
        causal_training_manifest_sha256="d" * 64,
    )
    payload = model.to_payload()

    assert payload["family"] == MODEL_FAMILY
    assert payload["target_semantics"] == TARGET_SEMANTICS
    assert payload["prediction_semantics"] == PREDICTION_SEMANTICS
    assert BinaryOutcomeMeanProbabilityModel.from_payload(payload) == model

    wrong_semantics = dict(payload)
    wrong_semantics["prediction_semantics"] = "generic-score-v1"
    with pytest.raises(BinaryProbabilityModelError, match="prediction semantics"):
        BinaryOutcomeMeanProbabilityModel.from_payload(wrong_semantics)

    wrong_count = dict(payload)
    wrong_count["success_count"] = 1
    with pytest.raises(BinaryProbabilityModelError, match="value mismatch|identity mismatch"):
        BinaryOutcomeMeanProbabilityModel.from_payload(wrong_count)


def test_probability_model_fails_closed_on_future_prediction_evidence() -> None:
    model = BinaryOutcomeMeanProbabilityModel(
        model_id="model-v1",
        training_cutoff="2026-01-02T00:00:00Z",
        success_count=1,
        training_count=2,
        causal_training_manifest_sha256="e" * 64,
    )

    with pytest.raises(BinaryProbabilityModelError, match="training cutoff"):
        model.predict_probability(
            observed_at="2026-01-01T00:00:00Z",
            decision_at="2026-01-01T12:00:00Z",
        )
    with pytest.raises(BinaryProbabilityModelError, match="not available"):
        model.predict_probability(
            observed_at="2026-01-03T00:00:00Z",
            decision_at="2026-01-02T12:00:00Z",
        )


def test_existing_walk_forward_runner_accepts_closed_probability_factory() -> None:
    points = (
        _point(
            "2026-01-01T00:00:00Z",
            1.0,
            revealed_at="2026-01-01T12:00:00Z",
            evidence_digit="a",
        ),
        _point(
            "2026-01-02T00:00:00Z",
            0.0,
            revealed_at="2026-01-02T12:00:00Z",
            evidence_digit="b",
        ),
        _point(
            "2026-01-03T00:00:00Z",
            1.0,
            revealed_at="2026-01-03T12:00:00Z",
            evidence_digit="c",
        ),
        _point(
            "2026-01-04T00:00:00Z",
            0.0,
            revealed_at="2026-01-04T12:00:00Z",
            evidence_digit="d",
        ),
    )

    result = WalkForwardRunner.run(
        points,
        minimum_train_size=2,
        model_factory=BinaryOutcomeMeanProbabilityFactory(),
    )

    assert result.model_family == MODEL_FAMILY
    assert result.primary_metric == "mse"
    assert result.folds
    assert all(0.0 <= fold.prediction <= 1.0 for fold in result.folds)
    assert all(fold.target in (0.0, 1.0) for fold in result.folds)
