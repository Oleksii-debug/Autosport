from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from autosport import predictive_authority as _runtime_authority
from autosport.forecasting import ForecastRecord, parse_iso_timestamp
from autosport.opportunity import ForecastRef, PredictiveEligibilityEvidence, QuoteRef
from autosport.predictive_authority import (
    resolve_authoritative_forecast_ref,
    resolve_forecast_predictive_authority,
)
from autosport.predictive_qualification import (
    ForecastCalibrationQualification,
    PredictiveAdmissionPolicy,
    PredictiveQualificationError,
)
from autosport.scientific_registry import (
    EvaluationBundleRef,
    ExperimentRecord,
    PromotionAction,
    PromotionDecision,
    ResearchOutcome,
    ScientificRegistry,
)


_HELPER_PATH = Path(__file__).with_name("test_scientific_registry.py")
_SPEC = importlib.util.spec_from_file_location(
    "_autosport_scientific_registry_helpers",
    _HELPER_PATH,
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load scientific-registry test helpers")
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)


_DECISION_TIME = "2026-01-04T00:02:00+00:00"
_FORECAST_TIME = "2026-01-04T00:01:00+00:00"
_AUTHORITY_MISSING = (
    "predictive eligibility was not resolved from canonical "
    "ScientificRegistry authority for this decision"
)


def _policy() -> PredictiveAdmissionPolicy:
    return PredictiveAdmissionPolicy(
        policy_id="predictive-authority-test-v1",
        canonical_strategy_id="canonical-strategy",
        required_estimand="roi",
        required_uncertainty_method="bootstrap intervals",
        minimum_effective_sample_size=3,
        maximum_uncertainty=Decimal("0.10"),
        maximum_calibration_error_upper=Decimal("0.05"),
        minimum_selective_coverage=Decimal("0.25"),
        maximum_selective_risk=Decimal("0.25"),
        maximum_evidence_age_seconds=172800,
        frozen_at=_HELPERS.T0,
    )


def _promotion_rule(policy: PredictiveAdmissionPolicy) -> str:
    payload = json.loads(_HELPERS._frozen_promotion_rule_text())
    payload["predictive_admission_policy_id"] = policy.policy_id
    payload["predictive_admission_policy_sha256"] = policy.sha256
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _qualification() -> ForecastCalibrationQualification:
    return ForecastCalibrationQualification(
        qualification_id="qualification-authority-v1",
        evaluation_bundle_id="eval-authority-v1",
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        research_protocol_id="protocol-1",
        dataset_snapshot_id="dataset-1",
        cohort_id="dataset-1",
        effective_sample_size=5,
        calibration_error=Decimal("0.01"),
        calibration_error_upper=Decimal("0.02"),
        selective_coverage=Decimal("0.80"),
        selective_risk=Decimal("0.10"),
        uncertainty_method="bootstrap intervals",
        available_at=_HELPERS.T3,
    )


def _forecast() -> ForecastRecord:
    return ForecastRecord(
        quote_key=_quote().quote_key,
        probability=Decimal("0.56"),
        model_id="fixture-model",
        model_version="model-1",
        strategy_version="strategy-1",
        model_training_cutoff_ts=_HELPERS.T1,
        input_cutoff_ts="2026-01-03T12:00:00+00:00",
        generated_at=_FORECAST_TIME,
        uncertainty=Decimal("0.04"),
        market_snapshot_hash="a" * 64,
    )


def _quote() -> QuoteRef:
    return QuoteRef(
        event_id="event-authority",
        market_id="market-authority",
        selection_id="selection-authority",
        source_id="fixture-book",
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-01-04T00:01:30+00:00",
        source_ts="2026-01-04T00:01:29+00:00",
        ingest_ts="2026-01-04T00:01:31+00:00",
        market_event_hash="b" * 64,
        market_snapshot_hash="a" * 64,
        sport="soccer",
    )


def _promoted_registry(
    tmp_path,
    qualification: ForecastCalibrationQualification,
    policy: PredictiveAdmissionPolicy,
):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    foundation = _HELPERS._foundation_with_binding(
        registry,
        promotion_rule=_promotion_rule(policy),
    )
    protocol = foundation["protocol"]
    bundle = EvaluationBundleRef(
        "eval-authority-v1",
        "9" * 64,
        "8" * 64,
        "dataset-1",
        protocol.protocol_sha256,
        (qualification.sha256, _HELPERS.SHA_A),
        "2026-01-04T00:00:10+00:00",
        evaluated_strategy_version_id="strategy-1",
        evaluated_model_version_id="model-1",
        effective_sample_size=5,
        effect_interval_low="0.05",
        effect_interval_high="0.15",
        practical_improvement="0.1",
    )
    registry.append(bundle)
    registry.append(
        ExperimentRecord(
            "experiment-authority-v1",
            "protocol-1",
            "dataset-1",
            "features-1",
            "strategy-1",
            "eval-authority-v1",
            7,
            _HELPERS.SHA_B,
            ResearchOutcome.POSITIVE,
            _HELPERS.T2,
            model_version_id="model-1",
            completed_at="2026-01-04T00:00:20+00:00",
            notes="predictive authority positive fixture",
        )
    )
    evidence = _HELPERS._promotion_evidence(
        experiment_id="experiment-authority-v1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-authority-v1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=bundle.bundle_sha256,
        evidence_id="ignored-helper-id",
        created_at="2026-01-04T00:00:30+00:00",
        rollback_identity="NONE",
    )
    registry.append(evidence)
    registry.record_promotion(
        PromotionDecision(
            "promotion-authority-v1",
            PromotionAction.PROMOTE,
            "strategy-1",
            "protocol-1",
            protocol.protocol_sha256,
            "eval-authority-v1",
            bundle.bundle_sha256,
            "2026-01-04T00:00:40+00:00",
            candidate_model_version_id="model-1",
            promotion_evidence_id=evidence.promotion_evidence_id,
        )
    )
    return registry, bundle


def _ref(
    forecast: ForecastRecord,
    evidence: PredictiveEligibilityEvidence,
) -> ForecastRef:
    return ForecastRef(
        forecast_id=forecast.forecast_id,
        forecast_hash=forecast.canonical_hash,
        quote_key=forecast.quote_key,
        probability=forecast.probability,
        input_cutoff_ts=forecast.input_cutoff_ts,
        market_snapshot_hash=forecast.market_snapshot_hash or "a" * 64,
        quote_market_event_hash="b" * 64,
        model_id=forecast.model_id,
        model_version=forecast.model_version,
        strategy_version=forecast.strategy_version,
        uncertainty=forecast.uncertainty,
        predictive_eligibility=evidence,
    )


def test_self_attested_predictive_witness_cannot_authorize_positive_path() -> None:
    forecast = _forecast()
    self_attested = PredictiveEligibilityEvidence(
        evaluation_id="caller-evaluation",
        evaluation_sha256="1" * 64,
        protocol_sha256="2" * 64,
        admission_policy_sha256="3" * 64,
        model_id=forecast.model_id,
        model_version=forecast.model_version,
        strategy_version=forecast.strategy_version,
        uncertainty_kind="absolute_probability_radius_v1",
        sample_size=500,
        minimum_sample_size=3,
        maximum_uncertainty=Decimal("0.10"),
        as_of=_DECISION_TIME,
        valid_until=_DECISION_TIME,
    )
    reason = _ref(forecast, self_attested).predictive_eligibility_reason(
        parse_iso_timestamp(_DECISION_TIME),
        expected_model_id=forecast.model_id,
    )
    assert reason == _AUTHORITY_MISSING


def test_backdated_permissive_policy_not_precommitted_by_protocol_fails_closed(
    tmp_path,
) -> None:
    policy = _policy()
    qualification = _qualification()
    registry, _ = _promoted_registry(tmp_path, qualification, policy)
    permissive = replace(
        policy,
        maximum_uncertainty=Decimal("1"),
        maximum_calibration_error_upper=Decimal("1"),
        minimum_selective_coverage=Decimal("0"),
        maximum_selective_risk=Decimal("1"),
        maximum_evidence_age_seconds=999999999,
    )
    with pytest.raises(
        PredictiveQualificationError,
        match="was not precommitted by ResearchProtocol",
    ):
        resolve_forecast_predictive_authority(
            registry,
            _forecast(),
            decision_time=_DECISION_TIME,
            policy=permissive,
            qualification=qualification,
        )


def test_durable_resolution_mints_opaque_exact_object_authority_and_restart_fails_closed(
    tmp_path,
) -> None:
    policy = _policy()
    qualification = _qualification()
    registry, bundle = _promoted_registry(tmp_path, qualification, policy)
    forecast = _forecast()
    decision = parse_iso_timestamp(_DECISION_TIME)

    evidence = resolve_forecast_predictive_authority(
        registry,
        forecast,
        decision_time=_DECISION_TIME,
        policy=policy,
        qualification=qualification,
    )
    stored_bundle = registry.get("EvaluationBundle", bundle.record_id)
    assert stored_bundle is not None
    assert evidence.evaluation_sha256 == stored_bundle.record_sha256
    assert evidence.evaluation_sha256 != bundle.bundle_sha256
    assert evidence.as_of == _DECISION_TIME
    assert evidence.valid_until == _DECISION_TIME

    caller_ref = _ref(forecast, evidence)
    assert caller_ref.predictive_eligibility_reason(
        decision,
        expected_model_id=forecast.model_id,
    ) == _AUTHORITY_MISSING

    authorized = resolve_authoritative_forecast_ref(
        registry,
        forecast,
        _quote(),
        decision_time=_DECISION_TIME,
        policy=policy,
        qualification=qualification,
    )
    assert authorized.predictive_eligibility_reason(
        decision,
        expected_model_id=forecast.model_id,
    ) is None

    reconstructed = ForecastRef.from_dict(authorized.to_dict())
    assert reconstructed.predictive_eligibility_reason(
        decision,
        expected_model_id=forecast.model_id,
    ) == _AUTHORITY_MISSING
    assert not hasattr(_runtime_authority, "_RESOLVED_AUTHORITIES")
    assert not hasattr(_runtime_authority, "_authority_key_from_ref")
