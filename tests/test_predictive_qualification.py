from decimal import Decimal

import pytest

from autosport.forecasting import ForecastRecord
from autosport.predictive_qualification import (
    PredictiveAdmissionPolicy,
    PredictiveQualificationError,
    resolve_predictive_eligibility,
)
from autosport.scientific_registry import ScientificRegistry


def _policy() -> PredictiveAdmissionPolicy:
    return PredictiveAdmissionPolicy(
        policy_id="paper-predictive-v1",
        canonical_strategy_id="predictive-edge",
        required_estimand="forecast-selective-calibration-risk-v1",
        required_uncertainty_method="bootstrap-v1",
        minimum_effective_sample_size=100,
        maximum_uncertainty=Decimal("0.08"),
        maximum_evidence_age_seconds=86400,
    )


def _forecast(*, uncertainty: Decimal = Decimal("0.05"), generated_at: str = "2026-09-20T01:00:00Z") -> ForecastRecord:
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


def test_predictive_policy_digest_is_canonical_and_threshold_is_external() -> None:
    policy = _policy()
    assert len(policy.sha256) == 64
    assert policy.to_dict()["maximum_uncertainty"] == "0.08"
    assert policy.sha256 == _policy().sha256


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
        )
