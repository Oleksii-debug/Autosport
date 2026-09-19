from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol

from .integrity import atomic_write_json
from .strategy_experiment import ScientificProtocolBinding
from .workspace_lock import WorkspaceEconomicLock


_HEX = frozenset("0123456789abcdef")
_RECORD_TYPES = frozenset(
    {
        "ResearchQuestion",
        "Hypothesis",
        "ResearchProtocol",
        "DatasetSnapshot",
        "FeatureSet",
        "ModelVersion",
        "StrategyVersion",
        "EvaluationBundle",
        "Experiment",
        "PromotionDecision",
        "PromotionEvidence",
        "Postmortem",
        "DriftReference",
        "DriftObservation",
        "DriftFinding",
    }
)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ValueError(f"{name} must be a canonical SHA-256 hex string")
    return text


def _iso(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return text


def _instant(value: object, name: str) -> datetime:
    return datetime.fromisoformat(_iso(value, name).replace("Z", "+00:00")).astimezone(timezone.utc)


def _text_tuple(value: object, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    items = tuple(_text(item, f"{name} item") for item in value)
    if not allow_empty and not items:
        raise ValueError(f"{name} must not be empty")
    if len(items) != len(set(items)):
        raise ValueError(f"{name} must not contain duplicates")
    return items


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_decimal(value: object, name: str) -> str:
    text = _text(value, name)
    if any(char in text for char in "eE"):
        raise ValueError(f"{name} must use fixed-point decimal text")
    try:
        parsed = Decimal(text)
    except Exception as exc:
        raise ValueError(f"{name} must be a decimal string") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    normalized = format(parsed, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized in ("", "-0"):
        normalized = "0"
    return normalized



def _sha256_text(value: object, name: str) -> str:
    return hashlib.sha256(_text(value, name).encode("utf-8")).hexdigest()


def promotion_holdout_access_id(
    *,
    research_protocol_id: str,
    dataset_manifest_sha256: str,
    source_identity: str,
    license_identity: str,
    confirmation_trial_family_id: str,
) -> str:
    """Derive one stable holdout identity across renamed dataset snapshots."""
    return _digest(
        {
            "schema_version": 1,
            "research_protocol_id": _text(research_protocol_id, "research_protocol_id"),
            "dataset_manifest_sha256": _sha256(dataset_manifest_sha256, "dataset_manifest_sha256"),
            "source_identity": _text(source_identity, "source_identity"),
            "license_identity": _text(license_identity, "license_identity"),
            "confirmation_trial_family_id": _text(
                confirmation_trial_family_id, "confirmation_trial_family_id"
            ),
        }
    )

def _frozen_promotion_rule_payload(value: object) -> dict[str, Any]:
    text = _text(value, "binding.promotion_rule")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PromotionEvidenceError("frozen promotion rule is not canonical JSON") from exc
    if type(payload) is not dict or payload.get("kind") != "autosport-promotion-rule-v1":
        raise PromotionEvidenceError("frozen promotion rule kind is unsupported")
    primary_metric = payload.get("primary_metric")
    minimum_improvement = payload.get("minimum_improvement")
    minimum_effective_sample_size = payload.get("minimum_effective_sample_size")
    if type(primary_metric) is not str or not primary_metric:
        raise PromotionEvidenceError("frozen promotion rule lacks primary metric")
    if isinstance(minimum_improvement, bool) or not isinstance(minimum_improvement, (int, float)) or not math.isfinite(minimum_improvement):
        raise PromotionEvidenceError("frozen promotion rule minimum improvement is invalid")
    if isinstance(minimum_effective_sample_size, bool) or not isinstance(minimum_effective_sample_size, int) or minimum_effective_sample_size <= 0:
        raise PromotionEvidenceError("frozen promotion rule minimum effective sample size is invalid")
    return payload

def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


class ResearchOutcome(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NULL = "NULL"
    HARMFUL = "HARMFUL"
    INCONCLUSIVE = "INCONCLUSIVE"


class PromotionAction(StrEnum):
    PROMOTE = "PROMOTE"
    RETAIN = "RETAIN"
    REJECT = "REJECT"
    ROLLBACK = "ROLLBACK"


class ScientificRecord(Protocol):
    @property
    def record_type(self) -> str: ...
    @property
    def record_id(self) -> str: ...
    @property
    def available_at(self) -> str: ...
    def to_payload(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ResearchQuestion:
    question_id: str
    statement: str
    source_sha256: str
    created_at: str

    def __post_init__(self) -> None:
        _text(self.question_id, "question_id")
        _text(self.statement, "statement")
        _sha256(self.source_sha256, "source_sha256")
        _iso(self.created_at, "created_at")

    @property
    def record_type(self) -> str: return "ResearchQuestion"
    @property
    def record_id(self) -> str: return self.question_id
    @property
    def available_at(self) -> str: return self.created_at
    def to_payload(self) -> dict[str, Any]:
        return {"question_id": self.question_id, "statement": self.statement,
                "source_sha256": self.source_sha256.lower(), "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class Hypothesis:
    hypothesis_id: str
    research_question_id: str
    statement: str
    falsifiable_prediction: str
    failure_criteria: str
    primary_metric: str
    protective_metrics: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        for name in ("hypothesis_id", "research_question_id", "statement",
                     "falsifiable_prediction", "failure_criteria", "primary_metric"):
            _text(getattr(self, name), name)
        _text_tuple(self.protective_metrics, "protective_metrics", allow_empty=True)
        _iso(self.created_at, "created_at")

    @property
    def record_type(self) -> str: return "Hypothesis"
    @property
    def record_id(self) -> str: return self.hypothesis_id
    @property
    def available_at(self) -> str: return self.created_at
    def to_payload(self) -> dict[str, Any]:
        return {"hypothesis_id": self.hypothesis_id, "research_question_id": self.research_question_id,
                "statement": self.statement, "falsifiable_prediction": self.falsifiable_prediction,
                "failure_criteria": self.failure_criteria, "primary_metric": self.primary_metric,
                "protective_metrics": list(self.protective_metrics), "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class ResearchProtocol:
    binding: ScientificProtocolBinding
    source_sha256: str
    environment_sha256: str
    dataset_manifest_sha256: str
    available_at_utc: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ScientificProtocolBinding):
            raise ValueError("binding must be a ScientificProtocolBinding")
        _sha256(self.source_sha256, "source_sha256")
        _sha256(self.environment_sha256, "environment_sha256")
        _sha256(self.dataset_manifest_sha256, "dataset_manifest_sha256")
        _iso(self.available_at_utc, "available_at_utc")

    @property
    def record_type(self) -> str: return "ResearchProtocol"
    @property
    def record_id(self) -> str: return self.binding.research_protocol_id
    @property
    def available_at(self) -> str: return self.available_at_utc
    @property
    def protocol_sha256(self) -> str: return self.binding.binding_sha256
    def to_payload(self) -> dict[str, Any]:
        return {"research_protocol_id": self.record_id, "protocol_sha256": self.protocol_sha256,
                "binding": self.binding.canonical_dict(), "source_sha256": self.source_sha256.lower(),
                "environment_sha256": self.environment_sha256.lower(),
                "dataset_manifest_sha256": self.dataset_manifest_sha256.lower(),
                "available_at": self.available_at_utc}


@dataclass(frozen=True, slots=True)
class DatasetSnapshot:
    dataset_snapshot_id: str
    manifest_sha256: str
    source_identity: str
    license_identity: str
    causal_cutoff: str
    available_at_utc: str
    outcome_reveal_after: str | None = None

    def __post_init__(self) -> None:
        for name in ("dataset_snapshot_id", "source_identity", "license_identity"):
            _text(getattr(self, name), name)
        _sha256(self.manifest_sha256, "manifest_sha256")
        _iso(self.causal_cutoff, "causal_cutoff")
        _iso(self.available_at_utc, "available_at_utc")
        if self.outcome_reveal_after is not None:
            _iso(self.outcome_reveal_after, "outcome_reveal_after")

    @property
    def record_type(self) -> str: return "DatasetSnapshot"
    @property
    def record_id(self) -> str: return self.dataset_snapshot_id
    @property
    def available_at(self) -> str: return self.available_at_utc
    def to_payload(self) -> dict[str, Any]:
        return {"dataset_snapshot_id": self.dataset_snapshot_id,
                "manifest_sha256": self.manifest_sha256.lower(),
                "source_identity": self.source_identity, "license_identity": self.license_identity,
                "causal_cutoff": self.causal_cutoff, "available_at": self.available_at_utc,
                "outcome_reveal_after": self.outcome_reveal_after}


@dataclass(frozen=True, slots=True)
class FeatureSet:
    feature_set_id: str
    version: str
    definition_sha256: str
    source_sha256: str
    available_at_utc: str

    def __post_init__(self) -> None:
        _text(self.feature_set_id, "feature_set_id")
        _text(self.version, "version")
        _sha256(self.definition_sha256, "definition_sha256")
        _sha256(self.source_sha256, "source_sha256")
        _iso(self.available_at_utc, "available_at_utc")

    @property
    def record_type(self) -> str: return "FeatureSet"
    @property
    def record_id(self) -> str: return self.feature_set_id
    @property
    def available_at(self) -> str: return self.available_at_utc
    def to_payload(self) -> dict[str, Any]:
        return {"feature_set_id": self.feature_set_id, "version": self.version,
                "definition_sha256": self.definition_sha256.lower(),
                "source_sha256": self.source_sha256.lower(), "available_at": self.available_at_utc}


@dataclass(frozen=True, slots=True)
class ModelVersion:
    model_version_id: str
    model_family: str
    artifact_sha256: str
    source_sha256: str
    environment_sha256: str
    dataset_snapshot_id: str
    feature_set_id: str
    research_protocol_id: str
    seed: int
    config_sha256: str
    created_at: str
    predecessor_model_version_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("model_version_id", "model_family", "dataset_snapshot_id",
                     "feature_set_id", "research_protocol_id"):
            _text(getattr(self, name), name)
        for name in ("artifact_sha256", "source_sha256", "environment_sha256", "config_sha256"):
            _sha256(getattr(self, name), name)
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        _iso(self.created_at, "created_at")
        if self.predecessor_model_version_id is not None:
            _text(self.predecessor_model_version_id, "predecessor_model_version_id")

    @property
    def record_type(self) -> str: return "ModelVersion"
    @property
    def record_id(self) -> str: return self.model_version_id
    @property
    def available_at(self) -> str: return self.created_at
    def to_payload(self) -> dict[str, Any]:
        return {"model_version_id": self.model_version_id, "model_family": self.model_family,
                "artifact_sha256": self.artifact_sha256.lower(), "source_sha256": self.source_sha256.lower(),
                "environment_sha256": self.environment_sha256.lower(),
                "dataset_snapshot_id": self.dataset_snapshot_id, "feature_set_id": self.feature_set_id,
                "research_protocol_id": self.research_protocol_id, "seed": self.seed,
                "config_sha256": self.config_sha256.lower(), "created_at": self.created_at,
                "predecessor_model_version_id": self.predecessor_model_version_id}


@dataclass(frozen=True, slots=True)
class StrategyVersion:
    strategy_version_id: str
    canonical_strategy_id: str
    source_sha256: str
    environment_sha256: str
    config_sha256: str
    created_at: str
    model_version_id: str | None = None
    predecessor_strategy_version_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("strategy_version_id", "canonical_strategy_id"):
            _text(getattr(self, name), name)
        for name in ("source_sha256", "environment_sha256", "config_sha256"):
            _sha256(getattr(self, name), name)
        _iso(self.created_at, "created_at")
        for name in ("model_version_id", "predecessor_strategy_version_id"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)

    @property
    def record_type(self) -> str: return "StrategyVersion"
    @property
    def record_id(self) -> str: return self.strategy_version_id
    @property
    def available_at(self) -> str: return self.created_at
    def to_payload(self) -> dict[str, Any]:
        return {"strategy_version_id": self.strategy_version_id,
                "canonical_strategy_id": self.canonical_strategy_id,
                "source_sha256": self.source_sha256.lower(),
                "environment_sha256": self.environment_sha256.lower(),
                "config_sha256": self.config_sha256.lower(), "created_at": self.created_at,
                "model_version_id": self.model_version_id,
                "predecessor_strategy_version_id": self.predecessor_strategy_version_id}


@dataclass(frozen=True, slots=True)
class EvaluationBundleRef:
    evaluation_bundle_id: str
    bundle_sha256: str
    evaluator_source_sha256: str
    dataset_snapshot_id: str
    protocol_sha256: str
    artifact_hashes: tuple[str, ...]
    created_at: str
    evaluated_strategy_version_id: str | None = None
    evaluated_model_version_id: str | None = None
    effective_sample_size: int | None = None
    effect_interval_low: str | None = None
    effect_interval_high: str | None = None
    practical_improvement: str | None = None

    def __post_init__(self) -> None:
        _text(self.evaluation_bundle_id, "evaluation_bundle_id")
        _text(self.dataset_snapshot_id, "dataset_snapshot_id")
        for name in ("bundle_sha256", "evaluator_source_sha256", "protocol_sha256"):
            _sha256(getattr(self, name), name)
        for value in self.artifact_hashes:
            _sha256(value, "artifact_hash")
        if len(self.artifact_hashes) != len(set(value.lower() for value in self.artifact_hashes)):
            raise ValueError("artifact_hashes must not contain duplicates")
        _iso(self.created_at, "created_at")
        if self.evaluated_strategy_version_id is not None:
            _text(self.evaluated_strategy_version_id, "evaluated_strategy_version_id")
        if self.evaluated_model_version_id is not None:
            _text(self.evaluated_model_version_id, "evaluated_model_version_id")
        if self.effective_sample_size is not None:
            if (
                isinstance(self.effective_sample_size, bool)
                or not isinstance(self.effective_sample_size, int)
                or self.effective_sample_size <= 0
            ):
                raise ValueError("effective_sample_size must be a positive integer")
        interval_values = (
            self.effect_interval_low,
            self.effect_interval_high,
            self.practical_improvement,
        )
        if any(value is not None for value in interval_values):
            if any(value is None for value in interval_values):
                raise ValueError(
                    "effect interval and practical improvement must be stored together"
                )
            for name in (
                "effect_interval_low",
                "effect_interval_high",
                "practical_improvement",
            ):
                value = getattr(self, name)
                if _canonical_decimal(value, name) != value:
                    raise ValueError(f"{name} must be canonical decimal text")
            low = Decimal(self.effect_interval_low)
            high = Decimal(self.effect_interval_high)
            practical = Decimal(self.practical_improvement)
            if low > high:
                raise ValueError("effect interval low must not exceed high")
            if practical < low or practical > high:
                raise ValueError("practical improvement must lie inside effect interval")

    @property
    def record_type(self) -> str: return "EvaluationBundle"
    @property
    def record_id(self) -> str: return self.evaluation_bundle_id
    @property
    def available_at(self) -> str: return self.created_at
    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "bundle_sha256": self.bundle_sha256.lower(),
            "evaluator_source_sha256": self.evaluator_source_sha256.lower(),
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "protocol_sha256": self.protocol_sha256.lower(),
            "artifact_hashes": [value.lower() for value in self.artifact_hashes],
            "created_at": self.created_at,
            "evaluated_strategy_version_id": self.evaluated_strategy_version_id,
            "evaluated_model_version_id": self.evaluated_model_version_id,
        }
        if self.effective_sample_size is not None:
            payload["effective_sample_size"] = self.effective_sample_size
        if self.effect_interval_low is not None:
            payload["effect_interval_low"] = self.effect_interval_low
            payload["effect_interval_high"] = self.effect_interval_high
            payload["practical_improvement"] = self.practical_improvement
        return payload


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    experiment_id: str
    research_protocol_id: str
    dataset_snapshot_id: str
    feature_set_id: str
    strategy_version_id: str
    evaluation_bundle_id: str
    seed: int
    config_sha256: str
    outcome: ResearchOutcome
    created_at: str
    model_version_id: str | None = None
    completed_at: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        for name in ("experiment_id", "research_protocol_id", "dataset_snapshot_id",
                     "feature_set_id", "strategy_version_id", "evaluation_bundle_id"):
            _text(getattr(self, name), name)
        if self.model_version_id is not None:
            _text(self.model_version_id, "model_version_id")
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        _sha256(self.config_sha256, "config_sha256")
        if not isinstance(self.outcome, ResearchOutcome):
            raise ValueError("outcome must be a ResearchOutcome")
        created = _instant(self.created_at, "created_at")
        if self.completed_at is None:
            raise ValueError("completed_at is required for a final experiment outcome")
        completed = _instant(self.completed_at, "completed_at")
        if completed < created:
            raise ValueError("completed_at must not precede created_at")
        if type(self.notes) is not str:
            raise ValueError("notes must be a string")

    @property
    def record_type(self) -> str: return "Experiment"
    @property
    def record_id(self) -> str: return self.experiment_id
    @property
    def available_at(self) -> str: return self.completed_at or self.created_at
    @property
    def fingerprint(self) -> str:
        return _digest({"research_protocol_id": self.research_protocol_id,
                        "dataset_snapshot_id": self.dataset_snapshot_id,
                        "feature_set_id": self.feature_set_id,
                        "model_version_id": self.model_version_id,
                        "strategy_version_id": self.strategy_version_id,
                        "seed": self.seed, "config_sha256": self.config_sha256.lower()})
    def to_payload(self) -> dict[str, Any]:
        return {"experiment_id": self.experiment_id, "research_protocol_id": self.research_protocol_id,
                "dataset_snapshot_id": self.dataset_snapshot_id, "feature_set_id": self.feature_set_id,
                "model_version_id": self.model_version_id, "strategy_version_id": self.strategy_version_id,
                "evaluation_bundle_id": self.evaluation_bundle_id, "seed": self.seed,
                "config_sha256": self.config_sha256.lower(), "outcome": self.outcome.value,
                "created_at": self.created_at, "completed_at": self.completed_at,
                "fingerprint": self.fingerprint, "notes": self.notes}


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    promotion_decision_id: str
    action: PromotionAction
    candidate_strategy_version_id: str
    research_protocol_id: str
    protocol_sha256: str
    evaluation_bundle_id: str
    evaluation_bundle_sha256: str
    decided_at: str
    predecessor_strategy_version_id: str | None = None
    rollback_to_strategy_version_id: str | None = None
    candidate_model_version_id: str | None = None
    promotion_evidence_id: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        for name in ("promotion_decision_id", "candidate_strategy_version_id",
                     "research_protocol_id", "evaluation_bundle_id"):
            _text(getattr(self, name), name)
        if not isinstance(self.action, PromotionAction):
            raise ValueError("action must be a PromotionAction")
        _sha256(self.protocol_sha256, "protocol_sha256")
        _sha256(self.evaluation_bundle_sha256, "evaluation_bundle_sha256")
        _iso(self.decided_at, "decided_at")
        for name in ("predecessor_strategy_version_id", "rollback_to_strategy_version_id",
                     "candidate_model_version_id", "promotion_evidence_id"):
            if getattr(self, name) is not None:
                _text(getattr(self, name), name)
        if self.action is PromotionAction.ROLLBACK and self.rollback_to_strategy_version_id is None:
            raise ValueError("ROLLBACK requires rollback_to_strategy_version_id")
        if type(self.reason) is not str:
            raise ValueError("reason must be a string")

    @property
    def record_type(self) -> str: return "PromotionDecision"
    @property
    def record_id(self) -> str: return self.promotion_decision_id
    @property
    def available_at(self) -> str: return self.decided_at
    def to_payload(self) -> dict[str, Any]:
        return {"promotion_decision_id": self.promotion_decision_id, "action": self.action.value,
                "candidate_strategy_version_id": self.candidate_strategy_version_id,
                "candidate_model_version_id": self.candidate_model_version_id,
                "research_protocol_id": self.research_protocol_id,
                "protocol_sha256": self.protocol_sha256.lower(),
                "evaluation_bundle_id": self.evaluation_bundle_id,
                "evaluation_bundle_sha256": self.evaluation_bundle_sha256.lower(),
                "predecessor_strategy_version_id": self.predecessor_strategy_version_id,
                "rollback_to_strategy_version_id": self.rollback_to_strategy_version_id,
                "promotion_evidence_id": self.promotion_evidence_id,
                "reason": self.reason, "decided_at": self.decided_at}


class PromotionEvidenceValidity(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INCONCLUSIVE = "INCONCLUSIVE"
    INVALID = "INVALID"


class PromotionEvidenceDirection(StrEnum):
    LOWER_IS_BETTER = "LOWER_IS_BETTER"
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    promotion_evidence_id: str
    experiment_id: str
    research_protocol_id: str
    research_question_id: str
    hypothesis_id: str
    candidate_strategy_version_id: str
    candidate_model_version_id: str | None
    evaluation_bundle_id: str
    evaluation_bundle_sha256: str
    dataset_snapshot_id: str
    holdout_access_id: str
    confirmation_trial_family_id: str
    estimand: str
    direction: PromotionEvidenceDirection
    cohort_id: str
    effective_sample_size: int
    minimum_effective_sample_size: int
    effect_interval_low: str
    effect_interval_high: str
    practical_improvement: str
    guardrails_passed: bool
    validity: PromotionEvidenceValidity
    holdout_consumed: bool
    stopping_rule_sha256: str
    multiple_comparison_control_sha256: str
    rollback_identity: str
    uncertainty_method: str
    created_at: str

    def __post_init__(self) -> None:
        for name in (
            "promotion_evidence_id", "experiment_id", "research_protocol_id",
            "research_question_id", "hypothesis_id", "candidate_strategy_version_id",
            "evaluation_bundle_id", "dataset_snapshot_id", "holdout_access_id",
            "confirmation_trial_family_id", "estimand", "cohort_id",
            "rollback_identity", "uncertainty_method",
        ):
            _text(getattr(self, name), name)
        if self.candidate_model_version_id is not None:
            _text(self.candidate_model_version_id, "candidate_model_version_id")
        if not isinstance(self.direction, PromotionEvidenceDirection):
            raise ValueError("direction must be a PromotionEvidenceDirection")
        if not isinstance(self.validity, PromotionEvidenceValidity):
            raise ValueError("validity must be a PromotionEvidenceValidity")
        for name in ("evaluation_bundle_sha256", "stopping_rule_sha256", "multiple_comparison_control_sha256"):
            _sha256(getattr(self, name), name)
        _iso(self.created_at, "created_at")
        if not isinstance(self.guardrails_passed, bool):
            raise ValueError("guardrails_passed must be boolean")
        if not isinstance(self.holdout_consumed, bool):
            raise ValueError("holdout_consumed must be boolean")
        for name in ("effective_sample_size", "minimum_effective_sample_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("effect_interval_low", "effect_interval_high", "practical_improvement"):
            if _canonical_decimal(getattr(self, name), name) != getattr(self, name):
                raise ValueError(f"{name} must be canonical decimal text")
        low = Decimal(self.effect_interval_low)
        high = Decimal(self.effect_interval_high)
        practical = Decimal(self.practical_improvement)
        if low > high:
            raise ValueError("effect interval low must not exceed high")
        if practical < low or practical > high:
            raise ValueError("practical improvement must lie inside effect interval")
        expected_id = _digest(self.to_payload(include_id=False))
        if self.promotion_evidence_id != expected_id:
            raise ValueError("promotion_evidence_id does not match canonical evidence identity")

    @property
    def record_type(self) -> str:
        return "PromotionEvidence"

    @property
    def record_id(self) -> str:
        return self.promotion_evidence_id

    @property
    def available_at(self) -> str:
        return self.created_at

    def to_payload(self, *, include_id: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "experiment_id": self.experiment_id,
            "research_protocol_id": self.research_protocol_id,
            "research_question_id": self.research_question_id,
            "hypothesis_id": self.hypothesis_id,
            "candidate_strategy_version_id": self.candidate_strategy_version_id,
            "candidate_model_version_id": self.candidate_model_version_id,
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "evaluation_bundle_sha256": self.evaluation_bundle_sha256.lower(),
            "dataset_snapshot_id": self.dataset_snapshot_id,
            "holdout_access_id": self.holdout_access_id,
            "confirmation_trial_family_id": self.confirmation_trial_family_id,
            "estimand": self.estimand,
            "direction": self.direction.value,
            "cohort_id": self.cohort_id,
            "effective_sample_size": self.effective_sample_size,
            "minimum_effective_sample_size": self.minimum_effective_sample_size,
            "effect_interval_low": self.effect_interval_low,
            "effect_interval_high": self.effect_interval_high,
            "practical_improvement": self.practical_improvement,
            "guardrails_passed": self.guardrails_passed,
            "validity": self.validity.value,
            "holdout_consumed": self.holdout_consumed,
            "stopping_rule_sha256": self.stopping_rule_sha256.lower(),
            "multiple_comparison_control_sha256": self.multiple_comparison_control_sha256.lower(),
            "rollback_identity": self.rollback_identity,
            "uncertainty_method": self.uncertainty_method,
            "created_at": self.created_at,
        }
        if include_id:
            payload["promotion_evidence_id"] = self.promotion_evidence_id
        return payload


@dataclass(frozen=True, slots=True)
class Postmortem:
    postmortem_id: str
    experiment_id: str
    classification: ResearchOutcome
    finding: str
    retest_conditions: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        _text(self.postmortem_id, "postmortem_id")
        _text(self.experiment_id, "experiment_id")
        if not isinstance(self.classification, ResearchOutcome):
            raise ValueError("classification must be a ResearchOutcome")
        if self.classification is ResearchOutcome.POSITIVE:
            raise ValueError("positive experiments do not use negative-result postmortems")
        _text(self.finding, "finding")
        _text_tuple(self.retest_conditions, "retest_conditions")
        _iso(self.created_at, "created_at")

    @property
    def record_type(self) -> str: return "Postmortem"
    @property
    def record_id(self) -> str: return self.postmortem_id
    @property
    def available_at(self) -> str: return self.created_at
    def to_payload(self) -> dict[str, Any]:
        return {"postmortem_id": self.postmortem_id, "experiment_id": self.experiment_id,
                "classification": self.classification.value, "finding": self.finding,
                "retest_conditions": list(self.retest_conditions), "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    record_type: str
    record_id: str
    available_at: str
    payload: dict[str, Any]
    record_sha256: str

    @property
    def reveal_after(self) -> str | None:
        value = self.payload.get("outcome_reveal_after")
        return value if isinstance(value, str) else None


class ScientificRegistryError(RuntimeError):
    pass


class ConflictingScientificRecordError(ScientificRegistryError):
    pass


class DuplicateExperimentFingerprintError(ScientificRegistryError):
    pass


class PromotionEvidenceError(ScientificRegistryError):
    pass


class ScientificRegistry:
    """Durable immutable scientific-memory registry.

    The registry owns research/model/strategy identities and promotion history, not
    evaluation arithmetic, strategy execution, market truth, or scheduling. Writers
    serialize through the canonical workspace economic lock and publish atomically.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._read()
        except FileNotFoundError as exc:
            raise ValueError("scientific registry is missing") from exc

    @classmethod
    def initialize_pristine(cls, path: str | Path) -> "ScientificRegistry":
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with WorkspaceEconomicLock(target.parent):
            if target.exists():
                registry = cls(target)
            else:
                atomic_write_json(target, {"schema_version": cls.SCHEMA_VERSION, "records": []})
                registry = cls(target)
        return registry

    def _read(self) -> dict[str, Any]:
        raw = self.path.read_text(encoding="utf-8")
        try:
            state = json.loads(raw, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_nonfinite)
        except json.JSONDecodeError as exc:
            raise ValueError("scientific registry must be valid UTF-8 JSON") from exc
        if type(state) is not dict or state.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError("scientific registry schema_version mismatch")
        records = state.get("records")
        if type(records) is not list:
            raise ValueError("scientific registry records must be a list")
        seen: set[tuple[str, str]] = set()
        fingerprints: set[str] = set()
        for raw_entry in records:
            self._validate_entry(raw_entry)
            key = (raw_entry["record_type"], raw_entry["record_id"])
            if key in seen:
                raise ValueError("scientific registry contains duplicate record identity")
            seen.add(key)
            if raw_entry["record_type"] == "Experiment":
                fingerprint = raw_entry["payload"].get("fingerprint")
                if fingerprint in fingerprints:
                    # Historical explicit repeats are represented by allow_repeat and therefore may
                    # share a fingerprint. They remain detectable by lookup rather than invalidating
                    # restart. Do not reject the persisted state here.
                    pass
                fingerprints.add(fingerprint)
        return state

    @staticmethod
    def _validate_entry(raw_entry: object) -> None:
        if type(raw_entry) is not dict:
            raise ValueError("scientific registry entry must be an object")
        required = {"record_type", "record_id", "available_at", "payload", "record_sha256"}
        if set(raw_entry) != required:
            raise ValueError("scientific registry entry fields mismatch")
        if raw_entry["record_type"] not in _RECORD_TYPES:
            raise ValueError("scientific registry record_type is unsupported")
        _text(raw_entry["record_id"], "record_id")
        _iso(raw_entry["available_at"], "available_at")
        if type(raw_entry["payload"]) is not dict:
            raise ValueError("scientific registry payload must be an object")
        expected = _digest({"record_type": raw_entry["record_type"],
                            "record_id": raw_entry["record_id"],
                            "available_at": raw_entry["available_at"],
                            "payload": raw_entry["payload"]})
        if _sha256(raw_entry["record_sha256"], "record_sha256") != expected:
            raise ValueError("scientific registry record digest mismatch")

    @staticmethod
    def _entry(record: ScientificRecord) -> dict[str, Any]:
        if record.record_type not in _RECORD_TYPES:
            raise ValueError("unsupported scientific record type")
        record_id = _text(record.record_id, "record_id")
        available_at = _iso(record.available_at, "available_at")
        payload = record.to_payload()
        if type(payload) is not dict:
            raise ValueError("scientific record payload must be an object")
        envelope = {"record_type": record.record_type, "record_id": record_id,
                    "available_at": available_at, "payload": payload}
        envelope["record_sha256"] = _digest(envelope)
        return envelope

    def append(self, record: ScientificRecord, *, allow_repeat_experiment: bool = False) -> str:
        if record.record_type == "PromotionDecision":
            raise PromotionEvidenceError("promotion decisions must be recorded through record_promotion")
        return self._append(record, allow_repeat_experiment=allow_repeat_experiment)

    def _append(self, record: ScientificRecord, *, allow_repeat_experiment: bool = False) -> str:
        entry = self._entry(record)
        if type(allow_repeat_experiment) is not bool:
            raise ValueError("allow_repeat_experiment must be boolean")
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            return self._append_entry_locked(
                state,
                entry,
                allow_repeat_experiment=allow_repeat_experiment,
            )

    @staticmethod
    def _validate_promotion_evidence_causal_inputs(
        state: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> None:
        if evidence.get("record_type") != "PromotionEvidence":
            raise ValueError("causal promotion-evidence validation requires PromotionEvidence")
        payload = evidence.get("payload")
        if not isinstance(payload, Mapping):
            raise PromotionEvidenceError("promotion evidence payload is invalid")
        evaluation_bundle_id = payload.get("evaluation_bundle_id")
        experiment_id = payload.get("experiment_id")
        if not isinstance(evaluation_bundle_id, str) or not evaluation_bundle_id:
            raise PromotionEvidenceError("promotion evidence lacks evaluation bundle identity")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise PromotionEvidenceError("promotion evidence lacks experiment identity")

        entries = {
            (raw["record_type"], raw["record_id"]): raw
            for raw in state["records"]
        }
        bundle = entries.get(("EvaluationBundle", evaluation_bundle_id))
        if bundle is None:
            raise PromotionEvidenceError(
                "promotion evidence references missing EvaluationBundle"
            )
        experiment = entries.get(("Experiment", experiment_id))
        if experiment is None:
            raise PromotionEvidenceError(
                "promotion evidence references missing matching Experiment"
            )
        if experiment["payload"].get("evaluation_bundle_id") != evaluation_bundle_id:
            raise PromotionEvidenceError(
                "promotion evidence experiment/evaluation lineage mismatch"
            )

        evidence_at = _instant(
            evidence["available_at"], "PromotionEvidence.available_at"
        )
        if _instant(bundle["available_at"], "EvaluationBundle.available_at") > evidence_at:
            raise PromotionEvidenceError(
                "promotion evidence availability precedes referenced evaluation"
            )
        if _instant(experiment["available_at"], "Experiment.available_at") > evidence_at:
            raise PromotionEvidenceError(
                "promotion evidence availability precedes matching experiment completion"
            )

    def _append_entry_locked(
        self,
        state: dict[str, Any],
        entry: dict[str, Any],
        *,
        allow_repeat_experiment: bool = False,
    ) -> str:
        if entry["record_type"] == "PromotionEvidence":
            self._validate_promotion_evidence_causal_inputs(state, entry)
        for existing in state["records"]:
            if (existing["record_type"], existing["record_id"]) == (
                entry["record_type"], entry["record_id"]
            ):
                if existing["record_sha256"] == entry["record_sha256"]:
                    return entry["record_sha256"]
                raise ConflictingScientificRecordError(
                    f"conflicting immutable scientific record: {entry['record_type']}:{entry['record_id']}"
                )
        if entry["record_type"] == "Experiment":
            fingerprint = entry["payload"]["fingerprint"]
            evaluation_bundle_id = entry["payload"]["evaluation_bundle_id"]
            for existing in state["records"]:
                if (
                    existing["record_type"] == "Experiment"
                    and existing["payload"].get("evaluation_bundle_id") == evaluation_bundle_id
                    and existing["payload"].get("fingerprint") != fingerprint
                ):
                    raise ConflictingScientificRecordError(
                        "evaluation bundle is already bound to a different experiment fingerprint"
                    )
            if not allow_repeat_experiment and any(
                existing["record_type"] == "Experiment"
                and existing["payload"].get("fingerprint") == fingerprint
                for existing in state["records"]
            ):
                raise DuplicateExperimentFingerprintError(
                    "experiment fingerprint already has durable history; inspect negative/null results before repeating"
                )
        state["records"].append(entry)
        atomic_write_json(self.path, state)
        self._read()
        return entry["record_sha256"]

    def get(self, record_type: str, record_id: str) -> RegistryEntry | None:
        _text(record_type, "record_type")
        _text(record_id, "record_id")
        state = self._read()
        for entry in state["records"]:
            if entry["record_type"] == record_type and entry["record_id"] == record_id:
                return RegistryEntry(**entry)
        return None

    def causal_records(self, record_type: str, *, as_of: str) -> tuple[RegistryEntry, ...]:
        if record_type not in _RECORD_TYPES:
            raise ValueError("unsupported record_type")
        cutoff = _instant(as_of, "as_of")
        values: list[RegistryEntry] = []
        state = self._read()
        for raw in state["records"]:
            if raw["record_type"] != record_type:
                continue
            if raw["record_type"] == "PromotionEvidence":
                try:
                    self._validate_promotion_evidence_causal_inputs(state, raw)
                except PromotionEvidenceError:
                    continue
            available = _instant(raw["available_at"], "available_at")
            reveal = raw["payload"].get("outcome_reveal_after")
            if available > cutoff:
                continue
            if isinstance(reveal, str) and _instant(reveal, "outcome_reveal_after") > cutoff:
                continue
            values.append(RegistryEntry(**raw))
        values.sort(key=lambda item: (_instant(item.available_at, "available_at"), item.record_id))
        return tuple(values)

    def find_experiment_fingerprint(self, fingerprint: str) -> tuple[RegistryEntry, ...]:
        wanted = _sha256(fingerprint, "fingerprint")
        matches = [
            RegistryEntry(**raw)
            for raw in self._read()["records"]
            if raw["record_type"] == "Experiment"
            and raw["payload"].get("fingerprint") == wanted
        ]
        matches.sort(key=lambda item: (_instant(item.available_at, "available_at"), item.record_id))
        return tuple(matches)

    @staticmethod
    def _promotion_order_key(raw: Mapping[str, Any]) -> tuple[datetime, str]:
        return (
            _instant(raw["available_at"], "PromotionDecision.available_at"),
            _text(raw["record_id"], "PromotionDecision.record_id"),
        )

    @staticmethod
    def _strategy_key_from_state(state: Mapping[str, Any], strategy_version_id: str) -> str:
        for raw in state["records"]:
            if raw["record_type"] == "StrategyVersion" and raw["record_id"] == strategy_version_id:
                return _text(raw["payload"].get("canonical_strategy_id"), "canonical_strategy_id")
        raise PromotionEvidenceError(
            f"promotion history references missing StrategyVersion:{strategy_version_id}"
        )

    @staticmethod
    def _promotion_champion_from_state(
        state: dict[str, Any],
        *,
        through_key: tuple[datetime, str],
        canonical_strategy_id: str,
    ) -> str | None:
        wanted_key = _text(canonical_strategy_id, "canonical_strategy_id")
        decisions = [
            raw
            for raw in state["records"]
            if raw["record_type"] == "PromotionDecision"
            and ScientificRegistry._promotion_order_key(raw) <= through_key
            and ScientificRegistry._strategy_key_from_state(
                state, raw["payload"]["candidate_strategy_version_id"]
            ) == wanted_key
        ]
        decisions.sort(key=ScientificRegistry._promotion_order_key)
        champion: str | None = None
        for raw in decisions:
            payload = raw["payload"]
            action = PromotionAction(payload["action"])
            predecessor = payload.get("predecessor_strategy_version_id")
            if action is PromotionAction.PROMOTE:
                if predecessor != champion:
                    raise PromotionEvidenceError(
                        "durable promotion history predecessor does not match current context champion"
                    )
                champion = payload["candidate_strategy_version_id"]
            elif action is PromotionAction.ROLLBACK:
                if champion != payload["candidate_strategy_version_id"]:
                    raise PromotionEvidenceError(
                        "durable rollback candidate does not match current context champion"
                    )
                rollback_target = payload["rollback_to_strategy_version_id"]
                if ScientificRegistry._strategy_key_from_state(state, rollback_target) != wanted_key:
                    raise PromotionEvidenceError("durable rollback target crosses strategy context")
                champion = rollback_target
        return champion

    def record_promotion(self, decision: PromotionDecision) -> str:
        entry = self._entry(decision)
        with WorkspaceEconomicLock(self.path.parent):
            state = self._read()
            for existing in state["records"]:
                if (existing["record_type"], existing["record_id"]) == (
                    entry["record_type"], entry["record_id"]
                ):
                    if existing["record_sha256"] == entry["record_sha256"]:
                        return entry["record_sha256"]
                    raise ConflictingScientificRecordError(
                        f"conflicting immutable scientific record: {entry['record_type']}:{entry['record_id']}"
                    )

            entries = {(raw["record_type"], raw["record_id"]): raw for raw in state["records"]}
            decision_at = _instant(decision.decided_at, "decided_at")
            decision_key = (decision_at, decision.record_id)

            def require(kind: str, identity: str) -> dict[str, Any]:
                value = entries.get((kind, identity))
                if value is None:
                    raise PromotionEvidenceError(f"promotion evidence missing {kind}:{identity}")
                if _instant(value["available_at"], f"{kind}.available_at") > decision_at:
                    raise PromotionEvidenceError(f"promotion evidence {kind}:{identity} was not available at decision time")
                reveal = value["payload"].get("outcome_reveal_after")
                if isinstance(reveal, str) and _instant(reveal, f"{kind}.outcome_reveal_after") > decision_at:
                    raise PromotionEvidenceError(f"promotion evidence {kind}:{identity} was not causally revealed at decision time")
                return value

            strategy = require("StrategyVersion", decision.candidate_strategy_version_id)
            canonical_strategy_id = _text(
                strategy["payload"].get("canonical_strategy_id"), "canonical_strategy_id"
            )
            if any(
                raw["record_type"] == "PromotionDecision"
                and self._strategy_key_from_state(
                    state, raw["payload"]["candidate_strategy_version_id"]
                ) == canonical_strategy_id
                and self._promotion_order_key(raw) > decision_key
                for raw in state["records"]
            ):
                raise PromotionEvidenceError(
                    "promotion decision cannot be backdated before durable promotion history in its strategy context"
                )
            current_champion = self._promotion_champion_from_state(
                state,
                through_key=decision_key,
                canonical_strategy_id=canonical_strategy_id,
            )
            if decision.action is PromotionAction.PROMOTE:
                if decision.predecessor_strategy_version_id != current_champion:
                    raise PromotionEvidenceError(
                        "promotion predecessor does not match current context champion"
                    )
            elif decision.action is PromotionAction.ROLLBACK:
                if decision.candidate_strategy_version_id != current_champion:
                    raise PromotionEvidenceError(
                        "rollback candidate does not match current context champion"
                    )

            protocol = require("ResearchProtocol", decision.research_protocol_id)
            bundle = require("EvaluationBundle", decision.evaluation_bundle_id)
            binding = protocol["payload"].get("binding")
            if type(binding) is not dict:
                raise PromotionEvidenceError("durable protocol lacks scientific binding")
            question = require("ResearchQuestion", binding.get("research_question_id", ""))
            hypothesis = require("Hypothesis", binding.get("hypothesis_id", ""))
            if _digest(question["payload"]) != binding.get("research_question_sha256"):
                raise PromotionEvidenceError("research question hash does not match frozen protocol binding")
            if _digest(hypothesis["payload"]) != binding.get("hypothesis_sha256"):
                raise PromotionEvidenceError("hypothesis hash does not match frozen protocol binding")
            if protocol["payload"].get("protocol_sha256") != decision.protocol_sha256.lower():
                raise PromotionEvidenceError("promotion protocol hash does not match durable protocol")
            if bundle["payload"].get("protocol_sha256") != decision.protocol_sha256.lower():
                raise PromotionEvidenceError("evaluation bundle is bound to a different protocol")
            if bundle["payload"].get("bundle_sha256") != decision.evaluation_bundle_sha256.lower():
                raise PromotionEvidenceError("promotion evaluation bundle hash does not match durable bundle")
            if bundle["payload"].get("evaluated_strategy_version_id") != decision.candidate_strategy_version_id:
                raise PromotionEvidenceError("evaluation bundle strategy identity does not match candidate")
            if bundle["payload"].get("evaluated_model_version_id") != decision.candidate_model_version_id:
                raise PromotionEvidenceError("evaluation bundle model identity does not match candidate")
            dataset = require("DatasetSnapshot", bundle["payload"]["dataset_snapshot_id"])
            if dataset["payload"].get("manifest_sha256") != protocol["payload"].get("dataset_manifest_sha256"):
                raise PromotionEvidenceError("dataset manifest does not match frozen research protocol")
            if _instant(dataset["payload"].get("causal_cutoff"), "DatasetSnapshot.causal_cutoff") != _instant(
                binding.get("causal_cutoff"), "binding.causal_cutoff"
            ):
                raise PromotionEvidenceError("dataset causal cutoff does not match frozen research protocol")

            strategy_model_id = strategy["payload"].get("model_version_id")
            if strategy_model_id != decision.candidate_model_version_id:
                raise PromotionEvidenceError("candidate strategy/model lineage mismatch")
            model: dict[str, Any] | None = None
            if decision.candidate_model_version_id is not None:
                model = require("ModelVersion", decision.candidate_model_version_id)
            if decision.predecessor_strategy_version_id is not None:
                predecessor = require("StrategyVersion", decision.predecessor_strategy_version_id)
                if predecessor["payload"].get("canonical_strategy_id") != canonical_strategy_id:
                    raise PromotionEvidenceError("promotion predecessor crosses strategy context")
            if decision.action is PromotionAction.ROLLBACK:
                rollback_target = decision.rollback_to_strategy_version_id or ""
                rollback_strategy = require("StrategyVersion", rollback_target)
                if rollback_strategy["payload"].get("canonical_strategy_id") != canonical_strategy_id:
                    raise PromotionEvidenceError("rollback target crosses strategy context")
                prior_champions: set[str] = set()
                for raw in sorted(
                    (
                        raw
                        for raw in state["records"]
                        if raw["record_type"] == "PromotionDecision"
                        and self._strategy_key_from_state(
                            state, raw["payload"]["candidate_strategy_version_id"]
                        ) == canonical_strategy_id
                        and self._promotion_order_key(raw) < decision_key
                    ),
                    key=self._promotion_order_key,
                ):
                    payload = raw["payload"]
                    action = PromotionAction(payload["action"])
                    if action is PromotionAction.PROMOTE:
                        prior_champions.add(payload["candidate_strategy_version_id"])
                    elif action is PromotionAction.ROLLBACK:
                        prior_champions.add(payload["rollback_to_strategy_version_id"])
                if rollback_target == decision.candidate_strategy_version_id or rollback_target not in prior_champions:
                    raise PromotionEvidenceError("rollback target must be a distinct prior durable champion")

            matching_experiments: list[dict[str, Any]] = []
            for raw in state["records"]:
                if raw["record_type"] != "Experiment":
                    continue
                payload = raw["payload"]
                if (
                    payload.get("research_protocol_id") == decision.research_protocol_id
                    and payload.get("strategy_version_id") == decision.candidate_strategy_version_id
                    and payload.get("evaluation_bundle_id") == decision.evaluation_bundle_id
                    and payload.get("model_version_id") == decision.candidate_model_version_id
                ):
                    matching_experiments.append(raw)
            if not matching_experiments:
                raise PromotionEvidenceError("promotion has no durable matching experiment")
            if len(matching_experiments) != 1:
                raise PromotionEvidenceError("promotion evidence is ambiguous across multiple matching experiments")
            matching_experiment_entry = matching_experiments[0]
            if _instant(matching_experiment_entry["available_at"], "Experiment.available_at") > decision_at:
                raise PromotionEvidenceError("matching experiment was not complete at decision time")
            matching_experiment = matching_experiment_entry["payload"]
            if bundle["payload"].get("dataset_snapshot_id") != matching_experiment.get("dataset_snapshot_id"):
                raise PromotionEvidenceError("evaluation bundle/experiment dataset lineage mismatch")
            feature = require("FeatureSet", matching_experiment["feature_set_id"])
            if feature["payload"].get("version") != binding.get("feature_set_version"):
                raise PromotionEvidenceError("feature set version does not match frozen research protocol")
            frozen_config = _sha256(binding.get("code_config_sha256"), "binding.code_config_sha256")
            if matching_experiment.get("config_sha256") != frozen_config:
                raise PromotionEvidenceError("experiment config does not match frozen research protocol")
            if strategy["payload"].get("config_sha256") != frozen_config:
                raise PromotionEvidenceError("strategy config does not match frozen research protocol")
            if model is not None:
                if model["payload"].get("research_protocol_id") != matching_experiment.get("research_protocol_id"):
                    raise PromotionEvidenceError("candidate model/experiment protocol lineage mismatch")
                if model["payload"].get("dataset_snapshot_id") != matching_experiment.get("dataset_snapshot_id"):
                    raise PromotionEvidenceError("candidate model/experiment dataset lineage mismatch")
                if model["payload"].get("feature_set_id") != matching_experiment.get("feature_set_id"):
                    raise PromotionEvidenceError("candidate model/experiment feature lineage mismatch")
                if model["payload"].get("config_sha256") != frozen_config:
                    raise PromotionEvidenceError("model config does not match frozen research protocol")
                if model["payload"].get("seed") != matching_experiment.get("seed"):
                    raise PromotionEvidenceError("candidate model/experiment seed lineage mismatch")
            if decision.action is PromotionAction.PROMOTE:
                frozen_rule = binding.get("promotion_rule")
                if not isinstance(frozen_rule, str):
                    raise PromotionEvidenceError(
                        "PROMOTE requires a frozen typed promotion rule"
                    )
                frozen_rule_payload = _frozen_promotion_rule_payload(frozen_rule)
                if strategy["payload"].get("predecessor_strategy_version_id") != decision.predecessor_strategy_version_id:
                    raise PromotionEvidenceError("promotion predecessor does not match candidate strategy lineage")
                if matching_experiment.get("outcome") != ResearchOutcome.POSITIVE.value:
                    raise PromotionEvidenceError("PROMOTE requires a positive durable experiment outcome")
                dataset = require("DatasetSnapshot", matching_experiment["dataset_snapshot_id"])
                evidence_trial_family = f"{decision.research_protocol_id}:confirmation-trial-family"
                expected_holdout_access_id = promotion_holdout_access_id(
                    research_protocol_id=decision.research_protocol_id,
                    dataset_manifest_sha256=dataset["payload"].get("manifest_sha256"),
                    source_identity=dataset["payload"].get("source_identity"),
                    license_identity=dataset["payload"].get("license_identity"),
                    confirmation_trial_family_id=evidence_trial_family,
                )
                evidence_id = decision.promotion_evidence_id
                if not isinstance(evidence_id, str) or not evidence_id:
                    raise PromotionEvidenceError("PROMOTE requires typed PromotionEvidence")
                evidence = require("PromotionEvidence", evidence_id)
                self._validate_promotion_evidence_causal_inputs(state, evidence)
                ep = evidence["payload"]
                for raw in state["records"]:
                    if (
                        raw["record_type"] != "PromotionEvidence"
                        or raw["record_id"] == evidence_id
                    ):
                        continue
                    if (
                        _instant(
                            raw["available_at"],
                            "PromotionEvidence.available_at",
                        )
                        > decision_at
                    ):
                        continue
                    if (
                        raw["payload"].get("holdout_access_id")
                        == ep.get("holdout_access_id")
                    ):
                        raise PromotionEvidenceError(
                            "confirmation holdout access has already been consumed "
                            "or disclosed by prior evidence"
                        )
                for raw in state["records"]:
                    if raw["record_type"] != "PromotionDecision":
                        continue
                    prior_id = raw["payload"].get("promotion_evidence_id")
                    if prior_id == evidence_id:
                        raise PromotionEvidenceError("promotion evidence has already been consumed")
                    if prior_id:
                        prior = entries.get(("PromotionEvidence", prior_id))
                        if (
                            prior is not None
                            and prior["payload"].get("holdout_access_id")
                            == ep.get("holdout_access_id")
                        ):
                            raise PromotionEvidenceError(
                                "confirmation holdout access has already been consumed by another promotion"
                            )
                expected = {
                    "experiment_id": matching_experiment_entry["record_id"],
                    "research_protocol_id": decision.research_protocol_id,
                    "research_question_id": binding.get("research_question_id"),
                    "hypothesis_id": binding.get("hypothesis_id"),
                    "candidate_strategy_version_id": decision.candidate_strategy_version_id,
                    "candidate_model_version_id": decision.candidate_model_version_id,
                    "evaluation_bundle_id": decision.evaluation_bundle_id,
                    "evaluation_bundle_sha256": decision.evaluation_bundle_sha256.lower(),
                    "dataset_snapshot_id": matching_experiment.get("dataset_snapshot_id"),
                    "estimand": hypothesis["payload"].get("primary_metric"),
                    "direction": PromotionEvidenceDirection.LOWER_IS_BETTER.value,
                    "rollback_identity": decision.predecessor_strategy_version_id or "NONE",
                    "confirmation_trial_family_id": evidence_trial_family,
                    "holdout_access_id": expected_holdout_access_id,
                }
                for key, value in expected.items():
                    if ep.get(key) != value:
                        raise PromotionEvidenceError(f"promotion evidence {key} does not match frozen decision lineage")
                if ep.get("validity") != PromotionEvidenceValidity.ELIGIBLE.value:
                    raise PromotionEvidenceError("PROMOTE requires eligible PromotionEvidence")
                if ep.get("holdout_consumed") is not False:
                    raise PromotionEvidenceError("PROMOTE requires an unconsumed confirmation holdout")
                effective_n = ep.get("effective_sample_size")
                minimum_n = ep.get("minimum_effective_sample_size")
                frozen_minimum_n = frozen_rule_payload.get("minimum_effective_sample_size")
                if type(minimum_n) is not int or minimum_n != frozen_minimum_n:
                    raise PromotionEvidenceError("promotion evidence minimum effective sample size is not frozen")
                durable_effective_n = bundle["payload"].get("effective_sample_size")
                if type(durable_effective_n) is not int or durable_effective_n <= 0:
                    raise PromotionEvidenceError(
                        "PROMOTE requires durable effective sample size from the evaluation bundle"
                    )
                if type(effective_n) is not int or effective_n != durable_effective_n:
                    raise PromotionEvidenceError(
                        "promotion evidence effective sample size does not match durable evaluation"
                    )
                if effective_n < frozen_minimum_n:
                    raise PromotionEvidenceError("PROMOTE requires sufficient effective sample size")
                durable_low = bundle["payload"].get("effect_interval_low")
                durable_high = bundle["payload"].get("effect_interval_high")
                durable_practical = bundle["payload"].get("practical_improvement")
                if not all(
                    isinstance(value, str)
                    for value in (durable_low, durable_high, durable_practical)
                ):
                    raise PromotionEvidenceError(
                        "PROMOTE requires durable effect interval evidence from the evaluation bundle"
                    )
                for name, value in (
                    ("effect_interval_low", durable_low),
                    ("effect_interval_high", durable_high),
                    ("practical_improvement", durable_practical),
                ):
                    if _canonical_decimal(value, f"EvaluationBundle.{name}") != value:
                        raise PromotionEvidenceError(
                            f"durable evaluation {name} is not canonical decimal text"
                        )
                if ep.get("effect_interval_low") != durable_low:
                    raise PromotionEvidenceError(
                        "promotion evidence effect interval low does not match durable evaluation"
                    )
                if ep.get("effect_interval_high") != durable_high:
                    raise PromotionEvidenceError(
                        "promotion evidence effect interval high does not match durable evaluation"
                    )
                if ep.get("practical_improvement") != durable_practical:
                    raise PromotionEvidenceError(
                        "promotion evidence practical improvement does not match durable evaluation"
                    )
                if ep.get("guardrails_passed") is not True:
                    raise PromotionEvidenceError("PROMOTE requires passing guardrails")
                stopping_sha = _sha256_text(binding.get("stopping_rule"), "binding.stopping_rule")
                comparison_sha = _sha256_text(
                    binding.get("multiple_comparison_control"),
                    "binding.multiple_comparison_control",
                )
                if ep.get("stopping_rule_sha256") != stopping_sha:
                    raise PromotionEvidenceError("promotion evidence stopping rule is not frozen")
                if ep.get("multiple_comparison_control_sha256") != comparison_sha:
                    raise PromotionEvidenceError("promotion evidence multiple-comparison control is not frozen")
                if ep.get("uncertainty_method") != binding.get("uncertainty_method"):
                    raise PromotionEvidenceError("promotion evidence uncertainty method is not frozen")
                try:
                    low = Decimal(ep.get("effect_interval_low"))
                    practical = Decimal(ep.get("practical_improvement"))
                    frozen_minimum_improvement = Decimal(
                        str(frozen_rule_payload.get("minimum_improvement"))
                    )
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise PromotionEvidenceError(
                        "promotion evidence effect/threshold values must be canonical decimal text"
                    ) from exc
                if not low.is_finite() or not practical.is_finite() or not frozen_minimum_improvement.is_finite():
                    raise PromotionEvidenceError(
                        "promotion evidence effect/threshold values must be finite"
                    )
                if low <= 0 or practical <= 0:
                    raise PromotionEvidenceError("PROMOTE requires strictly positive observed improvement and effect interval")
                if low < frozen_minimum_improvement or practical < frozen_minimum_improvement:
                    raise PromotionEvidenceError("PROMOTE requires improvement clearing the frozen minimum")
            return self._append_entry_locked(state, entry)

    def champion_strategy(
        self,
        *,
        as_of: str,
        canonical_strategy_id: str | None = None,
    ) -> str | None:
        decisions = self.causal_records("PromotionDecision", as_of=as_of)
        state = self._read()
        if canonical_strategy_id is None:
            keys = {
                self._strategy_key_from_state(state, entry.payload["candidate_strategy_version_id"])
                for entry in decisions
            }
            if not keys:
                return None
            if len(keys) != 1:
                raise PromotionEvidenceError(
                    "canonical_strategy_id is required when multiple strategy contexts have promotion history"
                )
            canonical_strategy_id = next(iter(keys))
        wanted_key = _text(canonical_strategy_id, "canonical_strategy_id")
        champion: str | None = None
        for entry in decisions:
            payload = entry.payload
            if self._strategy_key_from_state(
                state, payload["candidate_strategy_version_id"]
            ) != wanted_key:
                continue
            action = PromotionAction(payload["action"])
            predecessor = payload.get("predecessor_strategy_version_id")
            if action is PromotionAction.PROMOTE:
                if predecessor != champion:
                    raise PromotionEvidenceError(
                        "promotion predecessor does not match current context champion"
                    )
                champion = payload["candidate_strategy_version_id"]
            elif action is PromotionAction.ROLLBACK:
                if champion != payload["candidate_strategy_version_id"]:
                    raise PromotionEvidenceError(
                        "rollback candidate does not match current context champion"
                    )
                rollback_target = payload["rollback_to_strategy_version_id"]
                if self._strategy_key_from_state(state, rollback_target) != wanted_key:
                    raise PromotionEvidenceError("rollback target crosses strategy context")
                champion = rollback_target
        return champion

    def reproducibility_bundle(self, experiment_id: str) -> dict[str, Any]:
        experiment = self.get("Experiment", experiment_id)
        if experiment is None:
            raise KeyError(experiment_id)
        payload = experiment.payload
        refs: dict[str, RegistryEntry] = {}
        for kind, field in (
            ("ResearchProtocol", "research_protocol_id"),
            ("DatasetSnapshot", "dataset_snapshot_id"),
            ("FeatureSet", "feature_set_id"),
            ("StrategyVersion", "strategy_version_id"),
            ("EvaluationBundle", "evaluation_bundle_id"),
        ):
            entry = self.get(kind, payload[field])
            if entry is None:
                raise ScientificRegistryError(f"experiment references missing {kind}")
            refs[kind] = entry
        model_id = payload.get("model_version_id")
        if model_id is not None:
            model = self.get("ModelVersion", model_id)
            if model is None:
                raise ScientificRegistryError("experiment references missing ModelVersion")
            refs["ModelVersion"] = model

        bundle = {
            "schema_version": 1,
            "kind": "autosport-scientific-reproducibility-reference-bundle",
            "experiment": {"id": experiment.record_id, "sha256": experiment.record_sha256,
                           "fingerprint": payload["fingerprint"], "seed": payload["seed"],
                           "config_sha256": payload["config_sha256"], "outcome": payload["outcome"]},
            "references": {
                kind: {"id": entry.record_id, "sha256": entry.record_sha256}
                for kind, entry in sorted(refs.items())
            },
            "artifact_hashes": refs["EvaluationBundle"].payload["artifact_hashes"],
            "truth": {
                "raw_dataset_copied": False,
                "promotion_claim": False,
                "real_money_execution": False,
            },
        }
        bundle["bundle_sha256"] = _digest(bundle)
        return bundle

    def export_reproducibility_bundle(self, experiment_id: str, path: str | Path) -> str:
        bundle = self.reproducibility_bundle(experiment_id)
        atomic_write_json(path, bundle)
        return bundle["bundle_sha256"]

