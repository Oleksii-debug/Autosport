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


def _policy() -> PredictiveAdmissionPolicy:
    return PredictiveAdmissionPolicy(
        policy_id="paper-predictive-v2",
        canonical_strategy_id="predictive-edge",
        required_estimand="forecast-selective-calibration-risk-v1",
        required_uncertainty_method="bootstrap-v1",
        minimum_effective_sample_size=100,
        maximum_uncertainty=Decimal("0.08"),
        maximum_calibration_error_upper=Decimal("0.04"),
        minimum_selective_coverage=Decimal("0.35"),
        maximum_selective_risk=Decimal("0.20"),
        maximum_evidence_age_seconds=86400,
    )


def _qualification(
    *,
    calibration_error_upper: Decimal = Decimal("0.03"),
    selective_coverage: Decimal = Decimal("0.50"),
    selective_risk: Decimal = Decimal("0.15"),
    available_at: str = "2026-09-20T00:58:00Z",
) -> ForecastCalibrationQualification:
    return ForecastCalibrationQualification(
        qualification_id="qualification-v1",
        evaluation_bundle_id="evaluation-v1",
        strategy_version_id="strategy-v1",
        model_version_id="model-v1",
        research_protocol_id="protocol-v1",
        dataset_snapshot_id="dataset-v1",
        cohort_id="soccer-match-odds-holdout-v1",
        effective_sample_size=150,
        calibration_error=Decimal("0.02"),
        calibration_error_upper=calibration_error_upper,
        selective_coverage=selective_coverage,
        selective_risk=selective_risk,
        uncertainty_method="bootstrap-v1",
        available_at=available_at,
    )


def _forecast(
    *,
    uncertainty: Decimal = Decimal("0.05"),
    generated_at: str = "2026-09-20T01:00:00Z",
) -> ForecastRecord:
    return ForecastRecord(
        quote_key="event|market|selection",
        probability=Decimal("0.61"),
        model_id="model-family",
        model_version="model-v1",
        strategy_version="strategy-v1",
        model_training_cutoff_ts="2026-09-19T00:00:00Z",
        input_cutoff_ts="2026-09-20T00:59:00Z",
        generated_at=generated_at,
        uncertainty=uncertainty,
        market_snapshot_hash="a" * 64,
    )


def test_predictive_policy_and_calibration_artifact_digests_are_canonical() -> None:
    policy = _policy()
    qualification = _qualification()
    assert len(policy.sha256) == 64
    assert len(qualification.sha256) == 64
    assert policy.to_dict()["schema_version"] == 2
    assert policy.to_dict()["maximum_uncertainty"] == "0.08"
    assert policy.to_dict()["maximum_calibration_error_upper"] == "0.04"
    assert qualification.to_dict()["calibration_error_upper"] == "0.03"
    assert policy.sha256 == _policy().sha256
    assert qualification.sha256 == _qualification().sha256


def test_calibration_artifact_rejects_nonconservative_interval() -> None:
    with pytest.raises(
        PredictiveQualificationError,
        match="calibration_error_upper must not be below calibration_error",
    ):
        _qualification(calibration_error_upper=Decimal("0.01"))


def test_empty_scientific_registry_fails_closed(tmp_path) -> None:
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    with pytest.raises(
        PredictiveQualificationError,
        match="strategy version is absent",
    ):
        resolve_predictive_eligibility(
            registry,
            _forecast(),
            decision_time="2026-09-20T01:01:00Z",
            policy=_policy(),
            qualification=_qualification(),
        )


def test_uncertainty_over_frozen_policy_fails_before_registry_lookup(tmp_path) -> None:
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    with pytest.raises(
        PredictiveQualificationError,
        match="uncertainty exceeds frozen policy threshold",
    ):
        resolve_predictive_eligibility(
            registry,
            _forecast(uncertainty=Decimal("0.081")),
            decision_time="2026-09-20T01:01:00Z",
            policy=_policy(),
            qualification=_qualification(),
        )


@pytest.mark.parametrize(
    ("qualification", "message"),
    (
        (
            _qualification(calibration_error_upper=Decimal("0.041")),
            "calibration reliability upper bound exceeds",
        ),
        (
            _qualification(selective_coverage=Decimal("0.349")),
            "selective coverage is below",
        ),
        (
            _qualification(selective_risk=Decimal("0.201")),
            "selective risk exceeds",
        ),
    ),
)
def test_selective_prediction_thresholds_fail_closed_before_registry_lookup(
    tmp_path,
    qualification: ForecastCalibrationQualification,
    message: str,
) -> None:
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    with pytest.raises(PredictiveQualificationError, match=message):
        resolve_predictive_eligibility(
            registry,
            _forecast(),
            decision_time="2026-09-20T01:01:00Z",
            policy=_policy(),
            qualification=qualification,
        )


def test_future_calibration_qualification_fails_closed(tmp_path) -> None:
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    with pytest.raises(
        PredictiveQualificationError,
        match="calibration qualification was not causally available",
    ):
        resolve_predictive_eligibility(
            registry,
            _forecast(),
            decision_time="2026-09-20T01:01:00Z",
            policy=_policy(),
            qualification=_qualification(available_at="2026-09-20T01:02:00Z"),
        )


def test_future_generated_forecast_fails_closed(tmp_path) -> None:
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    with pytest.raises(
        PredictiveQualificationError,
        match="generated after decision time",
    ):
        resolve_predictive_eligibility(
            registry,
            _forecast(generated_at="2026-09-20T01:02:00Z"),
            decision_time="2026-09-20T01:01:00Z",
            policy=_policy(),
            qualification=_qualification(),
        )
