from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol

from .scientific_registry import RegistryEntry

_HEX = frozenset("0123456789abcdef")


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


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _payload_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if type(value) is str and value and value == value.strip() else None


def _payload_sha(payload: Mapping[str, Any], key: str) -> str | None:
    try:
        return _sha256(payload.get(key), key)
    except ValueError:
        return None


class AblationComponent(StrEnum):
    DATA = "DATA"
    MODEL = "MODEL"
    THRESHOLD = "THRESHOLD"
    SIZING = "SIZING"
    EXECUTION = "EXECUTION"


class AblationAttributionStatus(StrEnum):
    ATTRIBUTABLE = "ATTRIBUTABLE"
    UNATTRIBUTED = "UNATTRIBUTED"


class ScientificRegistryReader(Protocol):
    def get(self, record_type: str, record_id: str) -> RegistryEntry | None: ...


def _entry(
    registry: ScientificRegistryReader,
    record_type: str,
    record_id: str | None,
) -> RegistryEntry | None:
    if record_id is None:
        return None
    value = registry.get(record_type, record_id)
    if value is None or value.record_type != record_type or value.record_id != record_id:
        return None
    return value


@dataclass(frozen=True, slots=True)
class ScientificCoreAuthorityFingerprints:
    data_sha256: str
    model_sha256: str
    strategy_non_model_sha256: str

    def __post_init__(self) -> None:
        for name in ("data_sha256", "model_sha256", "strategy_non_model_sha256"):
            _sha256(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class AblationAuthoritySnapshot:
    """Comparison witnesses; DATA/MODEL are revalidated against the registry."""

    data_sha256: str | None
    model_sha256: str | None
    threshold_sha256: str | None
    sizing_sha256: str | None
    execution_sha256: str | None
    source_evidence_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "data_sha256",
            "model_sha256",
            "threshold_sha256",
            "sizing_sha256",
            "execution_sha256",
        ):
            value = getattr(self, name)
            if value is not None:
                _sha256(value, name)
        _sha256(self.source_evidence_sha256, "source_evidence_sha256")

    def fingerprint(self, component: AblationComponent) -> str | None:
        return {
            AblationComponent.DATA: self.data_sha256,
            AblationComponent.MODEL: self.model_sha256,
            AblationComponent.THRESHOLD: self.threshold_sha256,
            AblationComponent.SIZING: self.sizing_sha256,
            AblationComponent.EXECUTION: self.execution_sha256,
        }[component]

    @property
    def snapshot_sha256(self) -> str:
        return _digest(
            {
                "schema_version": 1,
                "data_sha256": self.data_sha256,
                "model_sha256": self.model_sha256,
                "threshold_sha256": self.threshold_sha256,
                "sizing_sha256": self.sizing_sha256,
                "execution_sha256": self.execution_sha256,
                "source_evidence_sha256": self.source_evidence_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class AblationAttribution:
    control_experiment_id: str
    treatment_experiment_id: str
    declared_component: AblationComponent
    status: AblationAttributionStatus
    reason_codes: tuple[str, ...]
    control_evaluation_bundle_id: str | None
    treatment_evaluation_bundle_id: str | None
    estimand: str | None
    control_authority_snapshot_sha256: str
    treatment_authority_snapshot_sha256: str
    changed_components: tuple[AblationComponent, ...]

    @property
    def promotion_authorized(self) -> bool:
        return False

    @property
    def learning_update_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "control_experiment_id": self.control_experiment_id,
            "treatment_experiment_id": self.treatment_experiment_id,
            "declared_component": self.declared_component.value,
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "control_evaluation_bundle_id": self.control_evaluation_bundle_id,
            "treatment_evaluation_bundle_id": self.treatment_evaluation_bundle_id,
            "estimand": self.estimand,
            "control_authority_snapshot_sha256": self.control_authority_snapshot_sha256,
            "treatment_authority_snapshot_sha256": self.treatment_authority_snapshot_sha256,
            "changed_components": [item.value for item in self.changed_components],
            "promotion_authorized": False,
            "learning_update_authorized": False,
            "execution_authorized": False,
        }

    @property
    def attribution_id(self) -> str:
        return _digest(self.to_payload())


def derive_scientific_core_authorities(
    *, registry: ScientificRegistryReader, experiment_id: str
) -> ScientificCoreAuthorityFingerprints | None:
    """Resolve trusted DATA/MODEL lineage without conflating eval and training data."""

    experiment = _entry(registry, "Experiment", _text(experiment_id, "experiment_id"))
    if experiment is None:
        return None
    dataset_id = _payload_text(experiment.payload, "dataset_snapshot_id")
    feature_id = _payload_text(experiment.payload, "feature_set_id")
    strategy_id = _payload_text(experiment.payload, "strategy_version_id")
    dataset = _entry(registry, "DatasetSnapshot", dataset_id)
    feature = _entry(registry, "FeatureSet", feature_id)
    strategy = _entry(registry, "StrategyVersion", strategy_id)
    if dataset is None or feature is None or strategy is None:
        return None

    try:
        data_sha = _digest(
            {
                "dataset_snapshot_id": dataset.record_id,
                "dataset_record_sha256": _sha256(dataset.record_sha256, "dataset.record_sha256"),
                "feature_set_id": feature.record_id,
                "feature_record_sha256": _sha256(feature.record_sha256, "feature.record_sha256"),
            }
        )
    except ValueError:
        return None

    model_id = experiment.payload.get("model_version_id")
    if model_id is None:
        if strategy.payload.get("model_version_id") is not None:
            return None
        model_sha = _digest({"schema_version": 1, "model_version_id": None})
    else:
        model_id = _payload_text(experiment.payload, "model_version_id")
        if model_id is None or strategy.payload.get("model_version_id") != model_id:
            return None
        model = _entry(registry, "ModelVersion", model_id)
        if model is None:
            return None
        training_dataset_id = _payload_text(model.payload, "dataset_snapshot_id")
        model_feature_id = _payload_text(model.payload, "feature_set_id")
        if _entry(registry, "DatasetSnapshot", training_dataset_id) is None:
            return None
        if _entry(registry, "FeatureSet", model_feature_id) is None:
            return None
        try:
            model_sha = _digest(
                {
                    "model_version_id": model.record_id,
                    "model_record_sha256": _sha256(model.record_sha256, "model.record_sha256"),
                }
            )
        except ValueError:
            return None

    canonical_strategy_id = _payload_text(strategy.payload, "canonical_strategy_id")
    source_sha = _payload_sha(strategy.payload, "source_sha256")
    environment_sha = _payload_sha(strategy.payload, "environment_sha256")
    config_sha = _payload_sha(strategy.payload, "config_sha256")
    if None in (canonical_strategy_id, source_sha, environment_sha, config_sha):
        return None
    strategy_non_model_sha = _digest(
        {
            "canonical_strategy_id": canonical_strategy_id,
            "source_sha256": source_sha,
            "environment_sha256": environment_sha,
            "config_sha256": config_sha,
        }
    )
    return ScientificCoreAuthorityFingerprints(data_sha, model_sha, strategy_non_model_sha)


def _changed_components(
    control: AblationAuthoritySnapshot, treatment: AblationAuthoritySnapshot
) -> tuple[tuple[AblationComponent, ...], tuple[AblationComponent, ...]]:
    missing: list[AblationComponent] = []
    changed: list[AblationComponent] = []
    for component in AblationComponent:
        left, right = control.fingerprint(component), treatment.fingerprint(component)
        if left is None or right is None:
            missing.append(component)
        elif left != right:
            changed.append(component)
    return tuple(missing), tuple(changed)


def evaluate_ablation_attribution(
    *,
    registry: ScientificRegistryReader,
    control_experiment_id: str,
    treatment_experiment_id: str,
    declared_component: AblationComponent,
    control_authorities: AblationAuthoritySnapshot,
    treatment_authorities: AblationAuthoritySnapshot,
) -> AblationAttribution:
    """Fail-closed one-factor attribution evidence; never promotion/execution authority."""

    control_id = _text(control_experiment_id, "control_experiment_id")
    treatment_id = _text(treatment_experiment_id, "treatment_experiment_id")
    if not isinstance(declared_component, AblationComponent):
        raise ValueError("declared_component must be an AblationComponent")
    reasons: list[str] = []
    estimand = None
    control = _entry(registry, "Experiment", control_id)
    treatment = _entry(registry, "Experiment", treatment_id)
    if control is None:
        reasons.append("CONTROL_EXPERIMENT_MISSING")
    if treatment is None:
        reasons.append("TREATMENT_EXPERIMENT_MISSING")

    control_eval_id = _payload_text(control.payload, "evaluation_bundle_id") if control else None
    treatment_eval_id = _payload_text(treatment.payload, "evaluation_bundle_id") if treatment else None
    control_eval = _entry(registry, "EvaluationBundle", control_eval_id)
    treatment_eval = _entry(registry, "EvaluationBundle", treatment_eval_id)
    if control and control_eval is None:
        reasons.append("CONTROL_EVALUATION_MISSING")
    if treatment and treatment_eval is None:
        reasons.append("TREATMENT_EVALUATION_MISSING")

    protocol_id = None
    if control and treatment:
        left = _payload_text(control.payload, "research_protocol_id")
        right = _payload_text(treatment.payload, "research_protocol_id")
        if left is None or right is None:
            reasons.append("RESEARCH_PROTOCOL_ID_MISSING")
        elif left != right:
            reasons.append("RESEARCH_PROTOCOL_MISMATCH")
        else:
            protocol_id = left
        if control.payload.get("seed") != treatment.payload.get("seed"):
            reasons.append("RANDOM_SEED_MISMATCH")
        control_config = _payload_sha(control.payload, "config_sha256")
        treatment_config = _payload_sha(treatment.payload, "config_sha256")
        if control_config is None or treatment_config is None:
            reasons.append("EXPERIMENT_CONFIG_IDENTITY_MISSING")
        elif control_config != treatment_config:
            reasons.append("EXPERIMENT_CONFIG_MISMATCH")

    protocol = _entry(registry, "ResearchProtocol", protocol_id)
    if protocol_id and protocol is None:
        reasons.append("RESEARCH_PROTOCOL_MISSING")
    if protocol:
        protocol_sha = _payload_sha(protocol.payload, "protocol_sha256")
        binding = protocol.payload.get("binding")
        if protocol_sha is None or not isinstance(binding, Mapping):
            reasons.append("RESEARCH_PROTOCOL_BINDING_INVALID")
        else:
            hypothesis = _entry(registry, "Hypothesis", _payload_text(binding, "hypothesis_id"))
            estimand = _payload_text(hypothesis.payload, "primary_metric") if hypothesis else None
            if estimand is None:
                reasons.append("ESTIMAND_MISSING")
            for prefix, bundle in (("CONTROL", control_eval), ("TREATMENT", treatment_eval)):
                if bundle and bundle.payload.get("protocol_sha256") != protocol_sha:
                    reasons.append(f"{prefix}_EVALUATION_PROTOCOL_MISMATCH")

    for prefix, experiment, bundle in (
        ("CONTROL", control, control_eval),
        ("TREATMENT", treatment, treatment_eval),
    ):
        if experiment and bundle:
            checks = (
                ("dataset_snapshot_id", "dataset_snapshot_id", "DATASET"),
                ("evaluated_strategy_version_id", "strategy_version_id", "STRATEGY"),
                ("evaluated_model_version_id", "model_version_id", "MODEL"),
            )
            for bundle_key, experiment_key, label in checks:
                if bundle.payload.get(bundle_key) != experiment.payload.get(experiment_key):
                    reasons.append(f"{prefix}_EVALUATION_{label}_MISMATCH")

    if control_eval and treatment_eval:
        left = _payload_sha(control_eval.payload, "evaluator_source_sha256")
        right = _payload_sha(treatment_eval.payload, "evaluator_source_sha256")
        if left is None or right is None:
            reasons.append("EVALUATOR_IDENTITY_MISSING")
        elif left != right:
            reasons.append("EVALUATOR_IDENTITY_MISMATCH")

    control_core = derive_scientific_core_authorities(registry=registry, experiment_id=control_id)
    treatment_core = derive_scientific_core_authorities(registry=registry, experiment_id=treatment_id)
    for prefix, supplied, core in (
        ("CONTROL", control_authorities, control_core),
        ("TREATMENT", treatment_authorities, treatment_core),
    ):
        if core is None:
            reasons.append(f"{prefix}_CORE_AUTHORITY_UNRESOLVED")
        else:
            if supplied.data_sha256 != core.data_sha256:
                reasons.append(f"{prefix}_DATA_AUTHORITY_MISMATCH")
            if supplied.model_sha256 != core.model_sha256:
                reasons.append(f"{prefix}_MODEL_AUTHORITY_MISMATCH")

    if declared_component in {
        AblationComponent.THRESHOLD,
        AblationComponent.SIZING,
        AblationComponent.EXECUTION,
    }:
        reasons.append("COMPONENT_SUBAUTHORITY_UNRESOLVED")
    if control_core and treatment_core:
        if control_core.strategy_non_model_sha256 != treatment_core.strategy_non_model_sha256:
            reasons.append("NON_MODEL_STRATEGY_AUTHORITY_MISMATCH")

    missing, changed = _changed_components(control_authorities, treatment_authorities)
    if missing:
        reasons.append("COMPONENT_AUTHORITY_MISSING")
    elif not changed:
        reasons.append("NO_COMPONENT_CHANGED")
    elif changed != (declared_component,):
        reasons.append("NOT_EXACT_ONE_FACTOR_DELTA")

    if reasons:
        status = AblationAttributionStatus.UNATTRIBUTED
        reason_codes = tuple(dict.fromkeys(reasons))
    else:
        status = AblationAttributionStatus.ATTRIBUTABLE
        reason_codes = ("EXACT_ONE_FACTOR_DELTA",)
    return AblationAttribution(
        control_id,
        treatment_id,
        declared_component,
        status,
        reason_codes,
        control_eval_id,
        treatment_eval_id,
        estimand,
        control_authorities.snapshot_sha256,
        treatment_authorities.snapshot_sha256,
        changed,
    )
