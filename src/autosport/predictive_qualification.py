from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from .forecasting import ForecastRecord, parse_iso_timestamp
from .scientific_registry import (
    PromotionAction,
    PromotionEvidenceValidity,
    ScientificRegistry,
)


class PredictiveQualificationError(ValueError):
    """Raised when predictive evidence cannot authorize positive allocation."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PredictiveQualificationError(
            f"{name} must be a non-empty canonical string"
        )
    value.encode("utf-8")
    return value


def _bounded_decimal(value: object, name: str) -> Decimal:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value < 0
        or value > 1
    ):
        raise PredictiveQualificationError(
            f"{name} must be an exact Decimal between 0 and 1"
        )
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PredictiveQualificationError(f"{name} must be a positive integer")
    return value


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ForecastCalibrationQualification:
    """Typed selective-prediction/calibration artifact frozen by EvaluationBundle.

    This object is not an independent authority. Its canonical digest must already
    be present in the durable ScientificRegistry EvaluationBundle artifact hashes.
    """

    qualification_id: str
    evaluation_bundle_id: str
    strategy_version_id: str
    model_version_id: str
    research_protocol_id: str
    dataset_snapshot_id: str
    cohort_id: str
    effective_sample_size: int
    calibration_error: Decimal
    calibration_error_upper: Decimal
    selective_coverage: Decimal
    selective_risk: Decimal
    uncertainty_method: str
    available_at: str

    def __post_init__(self) -> None:
        for name in (
            "qualification_id",
            "evaluation_bundle_id",
            "strategy_version_id",
            "model_version_id",
            "research_protocol_id",
            "dataset_snapshot_id",
            "cohort_id",
            "uncertainty_method",
        ):
            _text(getattr(self, name), name)
        _positive_int(self.effective_sample_size, "effective_sample_size")
        calibration_error = _bounded_decimal(
            self.calibration_error, "calibration_error"
        )
        calibration_error_upper = _bounded_decimal(
            self.calibration_error_upper, "calibration_error_upper"
        )
        if calibration_error_upper < calibration_error:
            raise PredictiveQualificationError(
                "calibration_error_upper must not be below calibration_error"
            )
        _bounded_decimal(self.selective_coverage, "selective_coverage")
        _bounded_decimal(self.selective_risk, "selective_risk")
        parse_iso_timestamp(_text(self.available_at, "available_at"))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.forecast_calibration_qualification",
            "schema_version": 1,
            "qualification_id": self.qualification_id,
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "strategy_version_id": self.strategy_version_id,
            "model_version_id": self.model_version_id,
            "research_protocol_id": self.research_protocol_id,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "cohort_id": self.cohort_id,
            "effective_sample_size": self.effective_sample_size,
            "calibration_error": str(self.calibration_error),
            "calibration_error_upper": str(self.calibration_error_upper),
            "selective_coverage": str(self.selective_coverage),
            "selective_risk": str(self.selective_risk),
            "uncertainty_method": self.uncertainty_method,
            "available_at": self.available_at,
        }

    @property
    def sha256(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class PredictiveAdmissionPolicy:
    """Frozen external admission policy; registry records remain evidence authority."""

    policy_id: str
    canonical_strategy_id: str
    required_estimand: str
    required_uncertainty_method: str
    minimum_effective_sample_size: int
    maximum_uncertainty: Decimal
    maximum_calibration_error_upper: Decimal
    minimum_selective_coverage: Decimal
    maximum_selective_risk: Decimal
    maximum_evidence_age_seconds: int

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "canonical_strategy_id",
            "required_estimand",
            "required_uncertainty_method",
        ):
            _text(getattr(self, name), name)
        _positive_int(
            self.minimum_effective_sample_size,
            "minimum_effective_sample_size",
        )
        _bounded_decimal(self.maximum_uncertainty, "maximum_uncertainty")
        _bounded_decimal(
            self.maximum_calibration_error_upper,
            "maximum_calibration_error_upper",
        )
        _bounded_decimal(
            self.minimum_selective_coverage,
            "minimum_selective_coverage",
        )
        _bounded_decimal(self.maximum_selective_risk, "maximum_selective_risk")
        _positive_int(
            self.maximum_evidence_age_seconds,
            "maximum_evidence_age_seconds",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.predictive_admission_policy",
            "schema_version": 2,
            "policy_id": self.policy_id,
            "canonical_strategy_id": self.canonical_strategy_id,
            "required_estimand": self.required_estimand,
            "required_uncertainty_method": self.required_uncertainty_method,
            "minimum_effective_sample_size": self.minimum_effective_sample_size,
            "maximum_uncertainty": str(self.maximum_uncertainty),
            "maximum_calibration_error_upper": str(
                self.maximum_calibration_error_upper
            ),
            "minimum_selective_coverage": str(self.minimum_selective_coverage),
            "maximum_selective_risk": str(self.maximum_selective_risk),
            "maximum_evidence_age_seconds": self.maximum_evidence_age_seconds,
        }

    @property
    def sha256(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ResolvedPredictiveEligibility:
    """Audit reference that must be re-resolved before positive allocation."""

    policy_sha256: str
    qualification_id: str
    qualification_sha256: str
    strategy_record_id: str
    strategy_record_sha256: str
    model_record_id: str
    model_record_sha256: str
    promotion_decision_id: str
    promotion_decision_sha256: str
    promotion_evidence_id: str
    promotion_evidence_sha256: str
    evaluation_bundle_id: str
    evaluation_bundle_sha256: str
    protocol_id: str
    protocol_sha256: str
    protocol_record_sha256: str
    dataset_snapshot_id: str
    dataset_record_sha256: str
    effective_sample_size: int
    minimum_effective_sample_size: int
    maximum_uncertainty: Decimal
    evidence_available_at: str
    resolved_at: str
    valid_until: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.resolved_predictive_eligibility",
            "schema_version": 2,
            "policy_sha256": self.policy_sha256,
            "qualification_id": self.qualification_id,
            "qualification_sha256": self.qualification_sha256,
            "strategy_record_id": self.strategy_record_id,
            "strategy_record_sha256": self.strategy_record_sha256,
            "model_record_id": self.model_record_id,
            "model_record_sha256": self.model_record_sha256,
            "promotion_decision_id": self.promotion_decision_id,
            "promotion_decision_sha256": self.promotion_decision_sha256,
            "promotion_evidence_id": self.promotion_evidence_id,
            "promotion_evidence_sha256": self.promotion_evidence_sha256,
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "evaluation_bundle_sha256": self.evaluation_bundle_sha256,
            "protocol_id": self.protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "protocol_record_sha256": self.protocol_record_sha256,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_record_sha256": self.dataset_record_sha256,
            "effective_sample_size": self.effective_sample_size,
            "minimum_effective_sample_size": self.minimum_effective_sample_size,
            "maximum_uncertainty": str(self.maximum_uncertainty),
            "evidence_available_at": self.evidence_available_at,
            "resolved_at": self.resolved_at,
            "valid_until": self.valid_until,
        }


def _available_by(entry: Any, decision_time: object, name: str) -> None:
    if parse_iso_timestamp(entry.available_at) > decision_time:
        raise PredictiveQualificationError(
            f"{name} was not causally available at decision time"
        )


def resolve_predictive_eligibility(
    registry: ScientificRegistry,
    forecast: ForecastRecord,
    *,
    decision_time: str,
    policy: PredictiveAdmissionPolicy,
    qualification: ForecastCalibrationQualification,
) -> ResolvedPredictiveEligibility:
    """Resolve positive predictive eligibility only from durable registry truth.

    Missing, mismatched, stale, revoked, under-supported, or self-asserted evidence
    raises PredictiveQualificationError. The typed calibration/selective-prediction
    artifact is accepted only when its exact digest was frozen into the durable
    EvaluationBundle. Callers must translate failure to WAIT/ZERO.
    """

    if not isinstance(registry, ScientificRegistry):
        raise PredictiveQualificationError("registry must be ScientificRegistry")
    if not isinstance(forecast, ForecastRecord):
        raise PredictiveQualificationError("forecast must be ForecastRecord")
    if not isinstance(policy, PredictiveAdmissionPolicy):
        raise PredictiveQualificationError(
            "policy must be PredictiveAdmissionPolicy"
        )
    if not isinstance(qualification, ForecastCalibrationQualification):
        raise PredictiveQualificationError(
            "qualification must be ForecastCalibrationQualification"
        )

    decision = parse_iso_timestamp(_text(decision_time, "decision_time"))
    qualification_at = parse_iso_timestamp(qualification.available_at)
    if qualification_at > decision:
        raise PredictiveQualificationError(
            "calibration qualification was not causally available at decision time"
        )
    if parse_iso_timestamp(forecast.generated_at) > decision:
        raise PredictiveQualificationError(
            "forecast was generated after decision time"
        )
    if parse_iso_timestamp(forecast.input_cutoff_ts) > decision:
        raise PredictiveQualificationError("forecast input cutoff is from the future")
    if forecast.uncertainty > policy.maximum_uncertainty:
        raise PredictiveQualificationError(
            "forecast uncertainty exceeds frozen policy threshold"
        )
    if (
        qualification.calibration_error_upper
        > policy.maximum_calibration_error_upper
    ):
        raise PredictiveQualificationError(
            "calibration reliability upper bound exceeds frozen policy threshold"
        )
    if qualification.selective_coverage < policy.minimum_selective_coverage:
        raise PredictiveQualificationError(
            "selective coverage is below frozen policy threshold"
        )
    if qualification.selective_risk > policy.maximum_selective_risk:
        raise PredictiveQualificationError(
            "selective risk exceeds frozen policy threshold"
        )
    if qualification.uncertainty_method != policy.required_uncertainty_method:
        raise PredictiveQualificationError(
            "calibration qualification uncertainty method does not match frozen policy"
        )

    strategy = registry.get("StrategyVersion", forecast.strategy_version)
    if strategy is None:
        raise PredictiveQualificationError(
            "forecast strategy version is absent from ScientificRegistry"
        )
    _available_by(strategy, decision, "StrategyVersion")
    if strategy.payload.get("canonical_strategy_id") != policy.canonical_strategy_id:
        raise PredictiveQualificationError(
            "forecast strategy belongs to a different canonical strategy"
        )
    if strategy.payload.get("model_version_id") != forecast.model_version:
        raise PredictiveQualificationError("forecast strategy/model lineage mismatch")

    model = registry.get("ModelVersion", forecast.model_version)
    if model is None:
        raise PredictiveQualificationError(
            "forecast model version is absent from ScientificRegistry"
        )
    _available_by(model, decision, "ModelVersion")
    if model.payload.get("model_family") != forecast.model_id:
        raise PredictiveQualificationError(
            "forecast model family does not match durable ModelVersion"
        )

    try:
        champion = registry.champion_strategy(
            as_of=decision_time,
            canonical_strategy_id=policy.canonical_strategy_id,
        )
    except Exception as exc:
        raise PredictiveQualificationError(
            "durable promotion history is not resolvable"
        ) from exc
    if champion != forecast.strategy_version:
        raise PredictiveQualificationError(
            "forecast strategy is not the durable champion at decision time"
        )

    decisions = tuple(
        entry
        for entry in registry.causal_records(
            "PromotionDecision", as_of=decision_time
        )
        if entry.payload.get("action") == PromotionAction.PROMOTE.value
        and entry.payload.get("candidate_strategy_version_id")
        == forecast.strategy_version
        and entry.payload.get("candidate_model_version_id")
        == forecast.model_version
    )
    if not decisions:
        raise PredictiveQualificationError(
            "no causal durable PROMOTE decision authorizes forecast identity"
        )
    decision_entry = decisions[-1]
    promotion_evidence_id = decision_entry.payload.get("promotion_evidence_id")
    if type(promotion_evidence_id) is not str or not promotion_evidence_id:
        raise PredictiveQualificationError(
            "PROMOTE decision lacks durable PromotionEvidence"
        )

    evidence = registry.get("PromotionEvidence", promotion_evidence_id)
    if evidence is None:
        raise PredictiveQualificationError(
            "PromotionEvidence is missing from ScientificRegistry"
        )
    _available_by(evidence, decision, "PromotionEvidence")
    ep = evidence.payload
    if ep.get("validity") != PromotionEvidenceValidity.ELIGIBLE.value:
        raise PredictiveQualificationError(
            "predictive PromotionEvidence is not ELIGIBLE"
        )
    if ep.get("guardrails_passed") is not True:
        raise PredictiveQualificationError(
            "predictive PromotionEvidence guardrails did not pass"
        )
    if ep.get("estimand") != policy.required_estimand:
        raise PredictiveQualificationError(
            "predictive estimand does not match frozen admission policy"
        )
    if ep.get("uncertainty_method") != policy.required_uncertainty_method:
        raise PredictiveQualificationError(
            "predictive uncertainty method does not match frozen policy"
        )
    effective_n = ep.get("effective_sample_size")
    evidence_minimum = ep.get("minimum_effective_sample_size")
    if type(effective_n) is not int or type(evidence_minimum) is not int:
        raise PredictiveQualificationError(
            "predictive evidence effective sample size is invalid"
        )
    minimum_effective_sample_size = max(
        policy.minimum_effective_sample_size,
        evidence_minimum,
    )
    if effective_n < minimum_effective_sample_size:
        raise PredictiveQualificationError(
            "predictive evidence has insufficient effective sample size"
        )

    bundle_id = decision_entry.payload.get("evaluation_bundle_id")
    bundle = (
        registry.get("EvaluationBundle", bundle_id)
        if isinstance(bundle_id, str)
        else None
    )
    if bundle is None:
        raise PredictiveQualificationError("promotion EvaluationBundle is missing")
    _available_by(bundle, decision, "EvaluationBundle")
    if bundle.record_sha256 != decision_entry.payload.get(
        "evaluation_bundle_sha256"
    ):
        raise PredictiveQualificationError(
            "promotion EvaluationBundle digest mismatch"
        )
    if bundle.payload.get("evaluated_strategy_version_id") != forecast.strategy_version:
        raise PredictiveQualificationError("evaluation strategy identity mismatch")
    if bundle.payload.get("evaluated_model_version_id") != forecast.model_version:
        raise PredictiveQualificationError("evaluation model identity mismatch")
    if bundle.payload.get("effective_sample_size") != effective_n:
        raise PredictiveQualificationError(
            "evaluation/evidence effective sample size mismatch"
        )
    if qualification.sha256 not in tuple(bundle.payload.get("artifact_hashes", ())):
        raise PredictiveQualificationError(
            "calibration qualification digest is not frozen in EvaluationBundle"
        )
    if qualification.evaluation_bundle_id != bundle.record_id:
        raise PredictiveQualificationError(
            "calibration qualification evaluation bundle mismatch"
        )
    if qualification.strategy_version_id != forecast.strategy_version:
        raise PredictiveQualificationError(
            "calibration qualification strategy identity mismatch"
        )
    if qualification.model_version_id != forecast.model_version:
        raise PredictiveQualificationError(
            "calibration qualification model identity mismatch"
        )
    if qualification.effective_sample_size != effective_n:
        raise PredictiveQualificationError(
            "calibration qualification effective sample size mismatch"
        )
    if qualification_at > parse_iso_timestamp(bundle.available_at):
        raise PredictiveQualificationError(
            "calibration qualification was not available when EvaluationBundle was frozen"
        )

    protocol_id = decision_entry.payload.get("research_protocol_id")
    protocol = (
        registry.get("ResearchProtocol", protocol_id)
        if isinstance(protocol_id, str)
        else None
    )
    if protocol is None:
        raise PredictiveQualificationError("promotion ResearchProtocol is missing")
    _available_by(protocol, decision, "ResearchProtocol")
    protocol_sha256 = protocol.payload.get("protocol_sha256")
    if protocol_sha256 != decision_entry.payload.get("protocol_sha256"):
        raise PredictiveQualificationError("promotion protocol digest mismatch")
    binding = protocol.payload.get("binding")
    if (
        type(binding) is not dict
        or binding.get("uncertainty_method")
        != policy.required_uncertainty_method
    ):
        raise PredictiveQualificationError(
            "protocol uncertainty method does not match frozen policy"
        )
    if qualification.research_protocol_id != protocol.record_id:
        raise PredictiveQualificationError(
            "calibration qualification research protocol mismatch"
        )

    dataset_id = bundle.payload.get("dataset_snapshot_id")
    dataset = (
        registry.get("DatasetSnapshot", dataset_id)
        if isinstance(dataset_id, str)
        else None
    )
    if dataset is None:
        raise PredictiveQualificationError("evaluation DatasetSnapshot is missing")
    _available_by(dataset, decision, "DatasetSnapshot")
    if qualification.dataset_snapshot_id != dataset.record_id:
        raise PredictiveQualificationError(
            "calibration qualification dataset identity mismatch"
        )
    if parse_iso_timestamp(dataset.payload["causal_cutoff"]) > parse_iso_timestamp(
        forecast.input_cutoff_ts
    ):
        raise PredictiveQualificationError(
            "evaluation dataset causal cutoff is after forecast input cutoff"
        )

    evidence_at = parse_iso_timestamp(evidence.available_at)
    max_age = timedelta(seconds=policy.maximum_evidence_age_seconds)
    if decision - evidence_at > max_age:
        raise PredictiveQualificationError(
            "predictive promotion evidence is stale"
        )
    if decision - qualification_at > max_age:
        raise PredictiveQualificationError(
            "predictive calibration qualification is stale"
        )
    available_at = max(evidence_at, qualification_at)
    valid_until = min(evidence_at + max_age, qualification_at + max_age)

    return ResolvedPredictiveEligibility(
        policy_sha256=policy.sha256,
        qualification_id=qualification.qualification_id,
        qualification_sha256=qualification.sha256,
        strategy_record_id=strategy.record_id,
        strategy_record_sha256=strategy.record_sha256,
        model_record_id=model.record_id,
        model_record_sha256=model.record_sha256,
        promotion_decision_id=decision_entry.record_id,
        promotion_decision_sha256=decision_entry.record_sha256,
        promotion_evidence_id=evidence.record_id,
        promotion_evidence_sha256=evidence.record_sha256,
        evaluation_bundle_id=bundle.record_id,
        evaluation_bundle_sha256=bundle.record_sha256,
        protocol_id=protocol.record_id,
        protocol_sha256=protocol_sha256,
        protocol_record_sha256=protocol.record_sha256,
        dataset_snapshot_id=dataset.record_id,
        dataset_record_sha256=dataset.record_sha256,
        effective_sample_size=effective_n,
        minimum_effective_sample_size=minimum_effective_sample_size,
        maximum_uncertainty=policy.maximum_uncertainty,
        evidence_available_at=available_at.isoformat().replace("+00:00", "Z"),
        resolved_at=decision_time,
        valid_until=valid_until.isoformat().replace("+00:00", "Z"),
    )
