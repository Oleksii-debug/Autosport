from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.forecasting import ForecastRecord
from autosport.predictive_qualification import (
    ForecastCalibrationQualification,
    PredictiveAdmissionPolicy,
    PredictiveQualificationError,
    resolve_predictive_eligibility,
)
from autosport.scientific_registry import ScientificRegistry


_GENERATED_AT = "2026-09-20T01:00:00Z"
_DECISION_AT = "2026-09-20T01:01:00Z"


def _policy() -> PredictiveAdmissionPolicy:
    return PredictiveAdmissionPolicy(
        policy_id="uncertainty-semantics-test-v1",
        canonical_strategy_id="predictive-edge",
        required_estimand="forecast-selective-calibration-risk-v1",
        required_uncertainty_method="bootstrap-v1",
        minimum_effective_sample_size=100,
        maximum_uncertainty=Decimal("0.08"),
        maximum_calibration_error_upper=Decimal("0.04"),
        minimum_selective_coverage=Decimal("0.35"),
        maximum_selective_risk=Decimal("0.20"),
        maximum_evidence_age_seconds=86400,
        frozen_at="2026-09-20T00:57:00Z",
    )


def _qualification() -> ForecastCalibrationQualification:
    return ForecastCalibrationQualification(
        qualification_id="uncertainty-semantics-qualification-v1",
        evaluation_bundle_id="evaluation-v1",
        strategy_version_id="strategy-v1",
        model_version_id="model-v1",
        research_protocol_id="protocol-v1",
        dataset_snapshot_id="dataset-v1",
        cohort_id="holdout-v1",
        effective_sample_size=150,
        calibration_error=Decimal("0.02"),
        calibration_error_upper=Decimal("0.03"),
        selective_coverage=Decimal("0.50"),
        selective_risk=Decimal("0.15"),
        uncertainty_method="bootstrap-v1",
        available_at="2026-09-20T00:58:00Z",
    )


def _forecast(
    *,
    uncertainty: Decimal | None = None,
    uncertainty_semantics: str | None = None,
) -> ForecastRecord:
    kwargs: dict[str, object] = {}
    if uncertainty is not None:
        kwargs["uncertainty"] = uncertainty
    provenance: dict[str, object] = {}
    if uncertainty_semantics is not None:
        provenance["uncertainty_semantics"] = uncertainty_semantics
    return ForecastRecord(
        quote_key="event|market|selection",
        probability=Decimal("0.61"),
        model_id="model-family",
        model_version="model-v1",
        strategy_version="strategy-v1",
        model_training_cutoff_ts="2026-09-19T00:00:00Z",
        input_cutoff_ts="2026-09-20T00:59:00Z",
        generated_at=_GENERATED_AT,
        market_snapshot_hash="a" * 64,
        provenance=provenance,
        **kwargs,
    )


def _resolve(tmp_path, forecast: ForecastRecord) -> None:
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    resolve_predictive_eligibility(
        registry,
        forecast,
        decision_time=_DECISION_AT,
        policy=_policy(),
        qualification=_qualification(),
    )


def test_omitted_default_zero_uncertainty_cannot_masquerade_as_certainty(tmp_path) -> None:
    with pytest.raises(
        PredictiveQualificationError,
        match="default-zero uncertainty lacks probability-radius semantics",
    ):
        _resolve(tmp_path, _forecast())


def test_explicit_descriptive_rating_radius_cannot_authorize_probability_risk(
    tmp_path,
) -> None:
    with pytest.raises(
        PredictiveQualificationError,
        match="uncertainty semantics are not absolute_probability_radius_v1",
    ):
        _resolve(
            tmp_path,
            _forecast(
                uncertainty=Decimal("0.05"),
                uncertainty_semantics=(
                    "max-descriptive-rating-radius-not-probability-ci"
                ),
            ),
        )


def test_legacy_nonzero_uncertainty_keeps_existing_fail_closed_registry_order(
    tmp_path,
) -> None:
    with pytest.raises(
        PredictiveQualificationError,
        match="strategy version is absent",
    ):
        _resolve(tmp_path, _forecast(uncertainty=Decimal("0.05")))


def test_typed_probability_radius_reaches_existing_registry_authority(tmp_path) -> None:
    with pytest.raises(
        PredictiveQualificationError,
        match="strategy version is absent",
    ):
        _resolve(
            tmp_path,
            _forecast(
                uncertainty=Decimal("0.05"),
                uncertainty_semantics="absolute_probability_radius_v1",
            ),
        )
