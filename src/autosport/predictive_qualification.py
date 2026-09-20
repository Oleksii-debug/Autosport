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
        raise PredictiveQualificationError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _bounded_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0 or value > 1:
        raise PredictiveQualificationError(f"{name} must be an exact Decimal between 0 and 1")
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
class PredictiveAdmissionPolicy:
    """Frozen external policy; scientific records remain the evidence authority."""

    policy_id: str
    canonical_strategy_id: str
    required_estimand: str
    required_uncertainty_method: str
    minimum_effective_sample_size: int
    maximum_uncertainty: Decimal
    maximum_evidence_age_seconds: int

    def __post_init__(self) -> None:
        for name in (
            "policy_id",
            "canonical_strategy_id",
            "required_estimand",
            "required_uncertainty_method",
        ):
            _text(getattr(self, name), name)
        if (
            isinstance(self.minimum_effective_sample_size, bool)
            or not isinstance(self.minimum_effective_sample_size, int)
            or self.minimum_effective_sample_size <= 0
        ):
            raise PredictiveQualificationError(
                "minimum_effective_sample_size must be a positive integer"
            )
        _bounded_decimal(self.maximum_uncertainty, "maximum_uncertainty")
        if (
            isinstance(self.maximum_evidence_age_seconds, bool)
            or not isinstance(self.maximum_evidence_age_seconds, int)
            or self.maximum_evidence_age_seconds <= 0
        ):
            raise PredictiveQualificationError(
                "maximum_evidence_age_seconds must be a positive integer"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.predictive_admission_policy",
            "schema_version": 1,
            "policy_id": self.policy_id,
            "canonical_strategy_id": self.canonical_strategy_id,
            "required_estimand": self.required_estimand,
            "required_uncertainty_method": self.required_uncertainty_method,
            "minimum_effective_sample_size": self.minimum_effective_sample_size,
            "maximum_uncertainty": str(self.maximum_uncertainty),
            "maximum_evidence_age_seconds": self.maximum_evidence_age_seconds,
        }

    @property
    def sha256(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ResolvedPredictiveEligibility:
    """Audit reference that must be re-resolved before positive allocation."""

    policy_sha256: str
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
    protocol_record_sha256: str
    dataset_snapshot_id: str
    dataset_record_sha256: str
    effective_sample_size: int
    maximum_uncertainty: Decimal
    resolved_at: str
    valid_until: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.resolved_predictive_eligibility",
            "schema_version": 1,
            "policy_sha256": self.policy_sha256,
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
            "protocol_record_sha256": self.protocol_record_sha256,
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "dataset_record_sha256": self.dataset_record_sha256,
            "effective_sample_size": self.effective_sample_size,
            "maximum_uncertainty": str(self.maximum_uncertainty),
            "resolved_at": self.resolved_at,
            "valid_until": self.valid_until,
        }


def _available_by(entry: Any, decision_time: object, name: str) -> None:
    if parse_iso_timestamp(entry.available_at) > decision_time:
        raise PredictiveQualificationError(f"{name} was not causally available at decision time")


def resolve_predictive_eligibility(
    registry: ScientificRegistry,
    forecast: ForecastRecord,
    *,
    decision_time: str,
    policy: PredictiveAdmissionPolicy,
) -> ResolvedPredictiveEligibility:
    """Resolve positive predictive eligibility only from durable registry truth.

    Missing, mismatched, stale, revoked, insufficient, or self-asserted evidence
    raises PredictiveQualificationError. Callers must translate that failure to
    WAIT/ZERO rather than treating the serialized reference as authority.
    """

    if not isinstance(registry, ScientificRegistry):
        raise PredictiveQualificationError("registry must be ScientificRegistry")
    if not isinstance(forecast, ForecastRecord):
        raise PredictiveQualificationError("forecast must be ForecastRecord")
    if not isinstance(policy, PredictiveAdmissionPolicy):
        raise PredictiveQualificationError("policy must be PredictiveAdmissionPolicy")

    decision = parse_iso_timestamp(_text(decision_time, "decision_time"))
    if parse_iso_timestamp(forecast.generated_at) > decision:
        raise PredictiveQualificationError("forecast was generated after decision time")
    if parse_iso_timestamp(forecast.input_cutoff_ts) > decision:
        raise PredictiveQualificationError("forecast input cutoff is from the future")
    if forecast.uncertainty > policy.maximum_uncertainty:
        raise PredictiveQualificationError("forecast uncertainty exceeds frozen policy threshold")

    strategy = registry.get("StrategyVersion", forecast.strategy_version)
    if strategy is None:
        raise PredictiveQualificationError("forecast strategy version is absent from ScientificRegistry")
    _available_by(strategy, decision, "StrategyVersion")
    if strategy.payload.get("canonical_strategy_id") != policy.canonical_strategy_id:
        raise PredictiveQualificationError("forecast strategy belongs to a different canonical strategy")
    if strategy.payload.get("model_version_id") != forecast.model_version:
        raise PredictiveQualificationError("forecast strategy/model lineage mismatch")

    model = registry.get("ModelVersion", forecast.model_version)
    if model is None:
        raise PredictiveQualificationError("forecast model version is absent from ScientificRegistry")
    _available_by(model, decision, "ModelVersion")
    if model.payload.get("model_family") != forecast.model_id:
        raise PredictiveQualificationError("forecast model family does not match durable ModelVersion")

    try:
        champion = registry.champion_strategy(
            as_of=decision_time,
            canonical_strategy_id=policy.canonical_strategy_id,
        )
    except Exception as exc:
        raise PredictiveQualificationError("durable promotion history is not resolvable") from exc
    if champion != forecast.strategy_version:
        raise PredictiveQualificationError("forecast strategy is not the durable champion at decision time")

    decisions = tuple(
        entry
        for entry in registry.causal_records("PromotionDecision", as_of=decision_time)
        if entry.payload.get("action") == PromotionAction.PROMOTE.value
        and entry.payload.get("candidate_strategy_version_id") == forecast.strategy_version
        and entry.payload.get("candidate_model_version_id") == forecast.model_version
    )
    if not decisions:
        raise PredictiveQualificationError("no causal durable PROMOTE decision authorizes forecast identity")
    decision_entry = decisions[-1]
    promotion_evidence_id = decision_entry.payload.get("promotion_evidence_id")
    if type(promotion_evidence_id) is not str or not promotion_evidence_id:
        raise PredictiveQualificationError("PROMOTE decision lacks durable PromotionEvidence")

    evidence = registry.get("PromotionEvidence", promotion_evidence_id)
    if evidence is None:
        raise PredictiveQualificationError("PromotionEvidence is missing from ScientificRegistry")
    _available_by(evidence, decision, "PromotionEvidence")
    ep = evidence.payload
    if ep.get("validity") != PromotionEvidenceValidity.ELIGIBLE.value:
        raise PredictiveQualificationError("predictive PromotionEvidence is not ELIGIBLE")
    if ep.get("guardrails_passed") is not True:
        raise PredictiveQualificationError("predictive PromotionEvidence guardrails did not pass")
    if ep.get("estimand") != policy.required_estimand:
        raise PredictiveQualificationError("predictive estimand does not match frozen admission policy")
    if ep.get("uncertainty_method") != policy.required_uncertainty_method:
        raise PredictiveQualificationError("predictive uncertainty method does not match frozen policy")
    effective_n = ep.get("effective_sample_size")
    evidence_minimum = ep.get("minimum_effective_sample_size")
    if (
        type(effective_n) is not int
        or type(evidence_minimum) is not int
        or effective_n < max(policy.minimum_effective_sample_size, evidence_minimum)
    ):
        raise PredictiveQualificationError("predictive evidence has insufficient effective sample size")

    bundle_id = decision_entry.payload.get("evaluation_bundle_id")
    bundle = registry.get("EvaluationBundle", bundle_id) if isinstance(bundle_id, str) else None
    if bundle is None:
        raise PredictiveQualificationError("promotion EvaluationBundle is missing")
    _available_by(bundle, decision, "EvaluationBundle")
    if bundle.record_sha256 != decision_entry.payload.get("evaluation_bundle_sha256"):
        raise PredictiveQualificationError("promotion EvaluationBundle digest mismatch")
    if bundle.payload.get("evaluated_strategy_version_id") != forecast.strategy_version:
        raise PredictiveQualificationError("evaluation strategy identity mismatch")
    if bundle.payload.get("evaluated_model_version_id") != forecast.model_version:
        raise PredictiveQualificationError("evaluation model identity mismatch")
    if bundle.payload.get("effective_sample_size") != effective_n:
        raise PredictiveQualificationError("evaluation/evidence effective sample size mismatch")

    protocol_id = decision_entry.payload.get("research_protocol_id")
    protocol = registry.get("ResearchProtocol", protocol_id) if isinstance(protocol_id, str) else None
    if protocol is None:
        raise PredictiveQualificationError("promotion ResearchProtocol is missing")
    _available_by(protocol, decision, "ResearchProtocol")
    if protocol.payload.get("protocol_sha256") != decision_entry.payload.get("protocol_sha256"):
        raise PredictiveQualificationError("promotion protocol digest mismatch")
    binding = protocol.payload.get("binding")
    if type(binding) is not dict or binding.get("uncertainty_method") != policy.required_uncertainty_method:
        raise PredictiveQualificationError("protocol uncertainty method does not match frozen policy")

    dataset_id = bundle.payload.get("dataset_snapshot_id")
    dataset = registry.get("DatasetSnapshot", dataset_id) if isinstance(dataset_id, str) else None
    if dataset is None:
        raise PredictiveQualificationError("evaluation DatasetSnapshot is missing")
    _available_by(dataset, decision, "DatasetSnapshot")
    if parse_iso_timestamp(dataset.payload["causal_cutoff"]) > parse_iso_timestamp(forecast.input_cutoff_ts):
        raise PredictiveQualificationError("evaluation dataset causal cutoff is after forecast input cutoff")

    evidence_at = parse_iso_timestamp(evidence.available_at)
    max_age = timedelta(seconds=policy.maximum_evidence_age_seconds)
    if decision - evidence_at > max_age:
        raise PredictiveQualificationError("predictive calibration evidence is stale")
    valid_until = evidence_at + max_age

    return ResolvedPredictiveEligibility(
        policy_sha256=policy.sha256,
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
        protocol_record_sha256=protocol.record_sha256,
        dataset_snapshot_id=dataset.record_id,
        dataset_record_sha256=dataset.record_sha256,
        effective_sample_size=effective_n,
        maximum_uncertainty=policy.maximum_uncertainty,
        resolved_at=decision_time,
        valid_until=valid_until.isoformat().replace("+00:00", "Z"),
    )
