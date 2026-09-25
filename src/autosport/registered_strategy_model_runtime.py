"""Product-owned reconstruction of one promoted StrategyModelFactory model.

This module is deliberately narrower than live feature observation or forecast
issuance. It resolves the exact activated StrategyVersion through durable
ScientificRegistry promotion history, verifies its ModelVersion and immutable
factory artifact, and reconstructs only the built-in model family whose decoder
is owned by the product.
"""

from __future__ import annotations

import math
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Final

from ._strategy_model_factory_impl import MeanBaselineModel
from .scientific_registry import PromotionAction, RegistryEntry, ScientificRegistry
from .strategy_model_factory import (
    FactoryArtifactStore,
    WalkForwardEvaluationConfig,
    _registry_state_sha256,
)


SCIENTIFIC_REGISTRY_FILE: Final = "scientific_registry.json"
FACTORY_ARTIFACT_DIRECTORY: Final = "factory-artifacts"
SUPPORTED_MODEL_FAMILY: Final = "mean-baseline-v1"
_MODEL_ARTIFACT_FIELDS: Final = frozenset(
    {
        "schema_version",
        "family",
        "model_id",
        "training_cutoff",
        "mean_target",
        "training_count",
        "identity_sha256",
        "model_version_id",
        "research_protocol_id",
        "dataset_snapshot_id",
        "feature_set_id",
        "config_sha256",
        "evaluator_config_sha256",
        "training_points_manifest_sha256",
        "seed",
    }
)
_HEX: Final = frozenset("0123456789abcdef")


class RegisteredStrategyModelRuntimeError(RuntimeError):
    """Durable registered-strategy model authority is incomplete or incompatible."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise RegisteredStrategyModelRuntimeError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RegisteredStrategyModelRuntimeError(
            f"{name} must be UTF-8 text"
        ) from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise RegisteredStrategyModelRuntimeError(
            f"{name} must be lowercase SHA-256"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegisteredStrategyModelRuntimeError(
            f"{name} must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise RegisteredStrategyModelRuntimeError(
            f"{name} must include timezone"
        )
    return parsed


def _make_registered_strategy_runtime_issuance_gate():
    issuance_token = object()
    issuance_context: ContextVar[object | None] = ContextVar(
        "autosport_registered_strategy_runtime_issuance",
        default=None,
    )

    def require_issuance() -> None:
        if issuance_context.get() is not issuance_token:
            raise RegisteredStrategyModelRuntimeError(
                "RegisteredStrategyModelRuntime must be issued by the canonical durable resolver"
            )

    def bind_resolver(implementation):
        implementation_code = implementation.__code__

        @wraps(implementation)
        def guarded_resolver(*args, **kwargs):
            if getattr(implementation, "__code__", None) is not implementation_code:
                raise RegisteredStrategyModelRuntimeError(
                    "registered-strategy runtime resolver authority changed"
                )
            marker = issuance_context.set(issuance_token)
            try:
                return implementation(*args, **kwargs)
            finally:
                issuance_context.reset(marker)

        return guarded_resolver

    return require_issuance, bind_resolver


(
    _require_registered_strategy_runtime_issuance,
    _bind_registered_strategy_runtime_resolver,
) = _make_registered_strategy_runtime_issuance_gate()


def _require_causal_entry(
    registry: ScientificRegistry,
    entry: RegistryEntry,
    *,
    as_of: str,
    name: str,
) -> None:
    for causal in registry.causal_records(entry.record_type, as_of=as_of):
        if causal.record_id != entry.record_id:
            continue
        if causal.record_sha256 != entry.record_sha256:
            raise RegisteredStrategyModelRuntimeError(
                f"{name} causal record identity mismatch"
            )
        return
    raise RegisteredStrategyModelRuntimeError(
        f"{name} was not causally available at as_of"
    )


def _require_precedes(
    registry: ScientificRegistry,
    earlier_type: str,
    earlier_id: str,
    later_type: str,
    later_id: str,
    *,
    name: str,
) -> None:
    if not registry.causal_precedes(
        earlier_type,
        earlier_id,
        later_type,
        later_id,
    ):
        raise RegisteredStrategyModelRuntimeError(
            f"{name} lacks durable registry append-order causality"
        )


def _promotion_authority(
    registry: ScientificRegistry,
    *,
    strategy_version_id: str,
    model_version_id: str,
    research_protocol_id: str,
    as_of: str,
) -> RegistryEntry:
    matches: list[RegistryEntry] = []
    for decision in registry.causal_records("PromotionDecision", as_of=as_of):
        payload = decision.payload
        if (
            payload.get("candidate_strategy_version_id") == strategy_version_id
            and payload.get("action") == PromotionAction.PROMOTE.value
        ):
            matches.append(decision)
    if not matches:
        raise RegisteredStrategyModelRuntimeError(
            "active strategy lacks durable PROMOTE authority"
        )
    authority = matches[-1]
    payload = authority.payload
    if (
        payload.get("candidate_model_version_id") != model_version_id
        or payload.get("research_protocol_id") != research_protocol_id
    ):
        raise RegisteredStrategyModelRuntimeError(
            "PROMOTE authority model/protocol lineage mismatch"
        )
    return authority


def _matching_positive_experiment(
    registry: ScientificRegistry,
    *,
    strategy_version_id: str,
    model_version_id: str,
    research_protocol_id: str,
    evaluation_bundle_id: str,
    as_of: str,
) -> RegistryEntry:
    matches = [
        entry
        for entry in registry.causal_records("Experiment", as_of=as_of)
        if entry.payload.get("strategy_version_id") == strategy_version_id
        and entry.payload.get("model_version_id") == model_version_id
        and entry.payload.get("research_protocol_id") == research_protocol_id
        and entry.payload.get("evaluation_bundle_id") == evaluation_bundle_id
    ]
    if len(matches) != 1:
        raise RegisteredStrategyModelRuntimeError(
            "promoted model must have exactly one causal matching Experiment"
        )
    experiment = matches[0]
    if experiment.payload.get("outcome") != "POSITIVE":
        raise RegisteredStrategyModelRuntimeError(
            "promoted model Experiment must have POSITIVE outcome"
        )
    return experiment


def _require_publication_registry_prefix(
    registry: ScientificRegistry,
    *,
    final_registry_sha256: str,
    model: RegistryEntry,
) -> None:
    """Bind a factory publication receipt to an authentic current registry prefix."""

    try:
        state = registry._read()
    except (OSError, ValueError) as exc:
        raise RegisteredStrategyModelRuntimeError(
            "ScientificRegistry cannot prove factory publication lineage"
        ) from exc
    records = state.get("records")
    if type(records) is not list:
        raise RegisteredStrategyModelRuntimeError(
            "ScientificRegistry records are invalid for publication binding"
        )

    matching_prefix: list[object] | None = None
    for end in range(1, len(records) + 1):
        prefix = {
            "schema_version": ScientificRegistry.SCHEMA_VERSION,
            "records": records[:end],
        }
        if _registry_state_sha256(prefix) == final_registry_sha256:
            matching_prefix = records[:end]
            break
    if matching_prefix is None:
        raise RegisteredStrategyModelRuntimeError(
            "factory publication final registry is not a current registry prefix"
        )

    if not any(
        type(raw) is dict
        and raw.get("record_type") == "ModelVersion"
        and raw.get("record_id") == model.record_id
        and raw.get("record_sha256") == model.record_sha256
        for raw in matching_prefix
    ):
        raise RegisteredStrategyModelRuntimeError(
            "factory publication registry prefix does not bind promoted ModelVersion"
        )


def _decode_mean_baseline_artifact(
    artifact: object,
    *,
    model: RegistryEntry,
    strategy: RegistryEntry,
    experiment: RegistryEntry,
) -> MeanBaselineModel:
    if type(artifact) is not dict or set(artifact) != _MODEL_ARTIFACT_FIELDS:
        raise RegisteredStrategyModelRuntimeError(
            "model artifact fields do not match the supported frozen schema"
        )
    if artifact.get("schema_version") != 1:
        raise RegisteredStrategyModelRuntimeError(
            "model artifact schema_version mismatch"
        )
    model_id = model.record_id
    model_payload = model.payload
    if (
        artifact.get("family") != SUPPORTED_MODEL_FAMILY
        or model_payload.get("model_family") != SUPPORTED_MODEL_FAMILY
    ):
        raise RegisteredStrategyModelRuntimeError(
            "model family is not supported by the product runtime decoder"
        )
    if (
        artifact.get("model_id") != model_id
        or artifact.get("model_version_id") != model_id
    ):
        raise RegisteredStrategyModelRuntimeError("model artifact identity mismatch")
    for artifact_field, model_field in (
        ("research_protocol_id", "research_protocol_id"),
        ("dataset_snapshot_id", "dataset_snapshot_id"),
        ("feature_set_id", "feature_set_id"),
        ("config_sha256", "config_sha256"),
        ("seed", "seed"),
    ):
        if artifact.get(artifact_field) != model_payload.get(model_field):
            raise RegisteredStrategyModelRuntimeError(
                f"model artifact {artifact_field} lineage mismatch"
            )
    if artifact.get("feature_set_id") != experiment.payload.get("feature_set_id"):
        raise RegisteredStrategyModelRuntimeError(
            "model artifact feature lineage does not match Experiment"
        )
    if artifact.get("dataset_snapshot_id") != experiment.payload.get(
        "dataset_snapshot_id"
    ):
        raise RegisteredStrategyModelRuntimeError(
            "model artifact dataset lineage does not match Experiment"
        )
    if artifact.get("config_sha256") != strategy.payload.get("config_sha256"):
        raise RegisteredStrategyModelRuntimeError(
            "model artifact config lineage does not match StrategyVersion"
        )
    _sha256(artifact.get("config_sha256"), "model artifact config_sha256")
    _sha256(
        artifact.get("evaluator_config_sha256"),
        "model artifact evaluator_config_sha256",
    )
    _sha256(
        artifact.get("training_points_manifest_sha256"),
        "model artifact training_points_manifest_sha256",
    )
    training_cutoff = _text(
        artifact.get("training_cutoff"),
        "model artifact training_cutoff",
    )
    _instant(training_cutoff, "model artifact training_cutoff")
    mean_target = artifact.get("mean_target")
    if (
        isinstance(mean_target, bool)
        or type(mean_target) not in (int, float)
        or not math.isfinite(float(mean_target))
    ):
        raise RegisteredStrategyModelRuntimeError(
            "model artifact mean_target must be finite numeric"
        )
    mean_target_value = float(mean_target)
    training_count = artifact.get("training_count")
    if (
        isinstance(training_count, bool)
        or not isinstance(training_count, int)
        or training_count <= 0
    ):
        raise RegisteredStrategyModelRuntimeError(
            "model artifact training_count must be positive integer"
        )
    rebuilt = MeanBaselineModel(
        model_id=model_id,
        training_cutoff=training_cutoff,
        mean_target=mean_target_value,
        training_count=training_count,
    )
    identity = _sha256(
        artifact.get("identity_sha256"),
        "model artifact identity_sha256",
    )
    if identity != rebuilt.identity_sha256:
        raise RegisteredStrategyModelRuntimeError(
            "model artifact executable identity mismatch"
        )
    return rebuilt


@dataclass(frozen=True, slots=True)
class RegisteredStrategyModelRuntime:
    """Exact durable model lineage plus the product-owned reconstructed executable."""

    strategy_version_id: str
    strategy_record_sha256: str
    model_version_id: str
    model_record_sha256: str
    model_artifact_sha256: str
    model_identity_sha256: str
    promotion_record_sha256: str
    experiment_id: str
    reproducibility_bundle_sha256: str
    canonical_strategy_id: str
    research_protocol_id: str
    dataset_snapshot_id: str
    feature_set_id: str
    authority_as_of: str
    training_cutoff: str
    _model: MeanBaselineModel = field(repr=False, compare=False)

    def __post_init__(
        self,
        _require_issuance=_require_registered_strategy_runtime_issuance,
    ) -> None:
        _require_issuance()
        _text(self.strategy_version_id, "strategy_version_id")
        _sha256(self.strategy_record_sha256, "strategy_record_sha256")
        _text(self.model_version_id, "model_version_id")
        _sha256(self.model_record_sha256, "model_record_sha256")
        _sha256(self.model_artifact_sha256, "model_artifact_sha256")
        _sha256(self.model_identity_sha256, "model_identity_sha256")
        _sha256(self.promotion_record_sha256, "promotion_record_sha256")
        _text(self.experiment_id, "experiment_id")
        _sha256(
            self.reproducibility_bundle_sha256,
            "reproducibility_bundle_sha256",
        )
        _text(self.canonical_strategy_id, "canonical_strategy_id")
        _text(self.research_protocol_id, "research_protocol_id")
        _text(self.dataset_snapshot_id, "dataset_snapshot_id")
        _text(self.feature_set_id, "feature_set_id")
        _instant(self.authority_as_of, "authority_as_of")
        _instant(self.training_cutoff, "training_cutoff")
        if type(self._model) is not MeanBaselineModel:
            raise RegisteredStrategyModelRuntimeError(
                "runtime executable must be the canonical MeanBaselineModel"
            )
        if self._model.model_id != self.model_version_id:
            raise RegisteredStrategyModelRuntimeError(
                "runtime executable model id does not match ModelVersion"
            )
        if self._model.identity_sha256 != self.model_identity_sha256:
            raise RegisteredStrategyModelRuntimeError(
                "runtime executable identity does not match durable model identity"
            )
        if self._model.training_cutoff != self.training_cutoff:
            raise RegisteredStrategyModelRuntimeError(
                "runtime executable training cutoff does not match durable lineage"
            )

    def predict_value(
        self,
        *,
        feature: float,
        observed_at: str,
        decision_at: str,
    ) -> float:
        """Evaluate the reconstructed generic model without asserting probability semantics."""

        if isinstance(feature, bool) or type(feature) not in (int, float):
            raise RegisteredStrategyModelRuntimeError(
                "live feature must be finite numeric"
            )
        numeric_feature = float(feature)
        if not math.isfinite(numeric_feature):
            raise RegisteredStrategyModelRuntimeError(
                "live feature must be finite numeric"
            )
        observed = _instant(observed_at, "feature observed_at")
        decision = _instant(decision_at, "decision_at")
        if observed > decision:
            raise RegisteredStrategyModelRuntimeError(
                "live feature is not available at decision time"
            )
        if _instant(self.training_cutoff, "model training_cutoff") > decision:
            raise RegisteredStrategyModelRuntimeError(
                "registered model training cutoff exceeds decision time"
            )
        authority = _instant(self.authority_as_of, "runtime authority_as_of")
        if decision != authority:
            raise RegisteredStrategyModelRuntimeError(
                "decision_at must equal runtime authority as_of"
            )
        value = self._model.mean_target
        if not math.isfinite(value):
            raise RegisteredStrategyModelRuntimeError(
                "registered model output must be finite"
            )
        return value


def resolve_registered_strategy_model(
    workspace: str | Path,
    *,
    strategy_version_id: str,
    as_of: str,
) -> RegisteredStrategyModelRuntime:
    """Reconstruct one exact active StrategyModelFactory model from durable authority."""

    workspace_path = Path(workspace)
    if not workspace_path.is_absolute():
        raise RegisteredStrategyModelRuntimeError(
            "workspace must be an absolute path"
        )
    strategy_id = _text(strategy_version_id, "strategy_version_id")
    decision_time = _instant(as_of, "as_of")

    registry = ScientificRegistry(workspace_path / SCIENTIFIC_REGISTRY_FILE)
    strategy = registry.get("StrategyVersion", strategy_id)
    if strategy is None:
        raise RegisteredStrategyModelRuntimeError(
            "activated StrategyVersion is missing"
        )
    _require_causal_entry(
        registry,
        strategy,
        as_of=as_of,
        name="StrategyVersion",
    )
    strategy_payload = strategy.payload
    if (
        strategy_payload.get("strategy_version_id") != strategy_id
        or strategy_payload.get("created_at") != strategy.available_at
    ):
        raise RegisteredStrategyModelRuntimeError(
            "StrategyVersion typed identity mismatch"
        )
    canonical_strategy_id = _text(
        strategy_payload.get("canonical_strategy_id"),
        "canonical_strategy_id",
    )
    champion_id = registry.champion_strategy(
        as_of=as_of,
        canonical_strategy_id=canonical_strategy_id,
    )
    if champion_id != strategy_id:
        raise RegisteredStrategyModelRuntimeError(
            "activated StrategyVersion is not the durable champion at as_of"
        )

    model_id = _text(
        strategy_payload.get("model_version_id"),
        "StrategyVersion model_version_id",
    )
    model = registry.get("ModelVersion", model_id)
    if model is None:
        raise RegisteredStrategyModelRuntimeError("champion ModelVersion is missing")
    _require_causal_entry(
        registry,
        model,
        as_of=as_of,
        name="ModelVersion",
    )
    model_payload = model.payload
    if (
        model_payload.get("model_version_id") != model_id
        or model_payload.get("created_at") != model.available_at
    ):
        raise RegisteredStrategyModelRuntimeError(
            "ModelVersion typed identity mismatch"
        )

    for field_name in ("source_sha256", "environment_sha256", "config_sha256"):
        strategy_value = _sha256(
            strategy_payload.get(field_name),
            f"StrategyVersion {field_name}",
        )
        model_value = _sha256(
            model_payload.get(field_name),
            f"ModelVersion {field_name}",
        )
        if strategy_value != model_value:
            raise RegisteredStrategyModelRuntimeError(
                f"strategy/model {field_name} lineage mismatch"
            )

    research_protocol_id = _text(
        model_payload.get("research_protocol_id"),
        "ModelVersion research_protocol_id",
    )
    dataset_snapshot_id = _text(
        model_payload.get("dataset_snapshot_id"),
        "ModelVersion dataset_snapshot_id",
    )
    feature_set_id = _text(
        model_payload.get("feature_set_id"),
        "ModelVersion feature_set_id",
    )
    protocol = registry.get("ResearchProtocol", research_protocol_id)
    dataset = registry.get("DatasetSnapshot", dataset_snapshot_id)
    feature = registry.get("FeatureSet", feature_set_id)
    if protocol is None or dataset is None or feature is None:
        raise RegisteredStrategyModelRuntimeError(
            "model foundation is incomplete in ScientificRegistry"
        )
    for entry, name in (
        (protocol, "ResearchProtocol"),
        (dataset, "DatasetSnapshot"),
        (feature, "FeatureSet"),
    ):
        _require_causal_entry(registry, entry, as_of=as_of, name=name)
    if protocol.payload.get("research_protocol_id") != research_protocol_id:
        raise RegisteredStrategyModelRuntimeError(
            "ResearchProtocol typed identity mismatch"
        )
    if dataset.payload.get("dataset_snapshot_id") != dataset_snapshot_id:
        raise RegisteredStrategyModelRuntimeError(
            "DatasetSnapshot typed identity mismatch"
        )
    if feature.payload.get("feature_set_id") != feature_set_id:
        raise RegisteredStrategyModelRuntimeError("FeatureSet typed identity mismatch")
    protocol_sha256 = _sha256(
        protocol.payload.get("protocol_sha256"),
        "ResearchProtocol protocol_sha256",
    )
    dataset_manifest_sha256 = _sha256(
        dataset.payload.get("manifest_sha256"),
        "DatasetSnapshot manifest_sha256",
    )
    protocol_dataset_manifest_sha256 = _sha256(
        protocol.payload.get("dataset_manifest_sha256"),
        "ResearchProtocol dataset_manifest_sha256",
    )
    if dataset_manifest_sha256 != protocol_dataset_manifest_sha256:
        raise RegisteredStrategyModelRuntimeError(
            "DatasetSnapshot manifest does not match ResearchProtocol"
        )
    protocol_binding = protocol.payload.get("binding")
    if type(protocol_binding) is not dict:
        raise RegisteredStrategyModelRuntimeError(
            "ResearchProtocol lacks frozen binding"
        )
    try:
        evaluation_config = WalkForwardEvaluationConfig.from_frozen_text(
            protocol_binding.get("evaluation_design")
        )
    except ValueError as exc:
        raise RegisteredStrategyModelRuntimeError(
            "ResearchProtocol evaluation_design is not canonical"
        ) from exc

    for earlier_type, earlier_id, label in (
        ("ResearchProtocol", research_protocol_id, "ResearchProtocol -> ModelVersion"),
        ("DatasetSnapshot", dataset_snapshot_id, "DatasetSnapshot -> ModelVersion"),
        ("FeatureSet", feature_set_id, "FeatureSet -> ModelVersion"),
    ):
        _require_precedes(
            registry,
            earlier_type,
            earlier_id,
            "ModelVersion",
            model_id,
            name=label,
        )
    _require_precedes(
        registry,
        "ModelVersion",
        model_id,
        "StrategyVersion",
        strategy_id,
        name="ModelVersion -> StrategyVersion",
    )

    promotion = _promotion_authority(
        registry,
        strategy_version_id=strategy_id,
        model_version_id=model_id,
        research_protocol_id=research_protocol_id,
        as_of=as_of,
    )
    _require_precedes(
        registry,
        "StrategyVersion",
        strategy_id,
        "PromotionDecision",
        promotion.record_id,
        name="StrategyVersion -> PromotionDecision",
    )
    if promotion.payload.get("protocol_sha256") != protocol_sha256:
        raise RegisteredStrategyModelRuntimeError(
            "PROMOTE authority protocol digest mismatch"
        )
    evaluation_bundle_id = _text(
        promotion.payload.get("evaluation_bundle_id"),
        "PromotionDecision evaluation_bundle_id",
    )
    evaluation = registry.get("EvaluationBundle", evaluation_bundle_id)
    if evaluation is None:
        raise RegisteredStrategyModelRuntimeError(
            "PROMOTE EvaluationBundle is missing"
        )
    _require_causal_entry(
        registry,
        evaluation,
        as_of=as_of,
        name="EvaluationBundle",
    )
    if (
        evaluation.payload.get("evaluation_bundle_id") != evaluation_bundle_id
        or evaluation.payload.get("evaluated_strategy_version_id") != strategy_id
        or evaluation.payload.get("evaluated_model_version_id") != model_id
        or evaluation.payload.get("dataset_snapshot_id") != dataset_snapshot_id
        or evaluation.payload.get("protocol_sha256") != protocol_sha256
        or evaluation.payload.get("bundle_sha256")
        != promotion.payload.get("evaluation_bundle_sha256")
    ):
        raise RegisteredStrategyModelRuntimeError(
            "PROMOTE EvaluationBundle lineage mismatch"
        )
    for earlier_type, earlier_id, label in (
        ("ModelVersion", model_id, "ModelVersion -> EvaluationBundle"),
        ("StrategyVersion", strategy_id, "StrategyVersion -> EvaluationBundle"),
    ):
        _require_precedes(
            registry,
            earlier_type,
            earlier_id,
            "EvaluationBundle",
            evaluation_bundle_id,
            name=label,
        )
    _require_precedes(
        registry,
        "EvaluationBundle",
        evaluation_bundle_id,
        "PromotionDecision",
        promotion.record_id,
        name="EvaluationBundle -> PromotionDecision",
    )

    experiment = _matching_positive_experiment(
        registry,
        strategy_version_id=strategy_id,
        model_version_id=model_id,
        research_protocol_id=research_protocol_id,
        evaluation_bundle_id=evaluation_bundle_id,
        as_of=as_of,
    )
    if (
        experiment.payload.get("dataset_snapshot_id") != dataset_snapshot_id
        or experiment.payload.get("feature_set_id") != feature_set_id
        or experiment.payload.get("config_sha256")
        != model_payload.get("config_sha256")
        or experiment.payload.get("seed") != model_payload.get("seed")
    ):
        raise RegisteredStrategyModelRuntimeError("Experiment model lineage mismatch")
    _require_precedes(
        registry,
        "EvaluationBundle",
        evaluation_bundle_id,
        "Experiment",
        experiment.record_id,
        name="EvaluationBundle -> Experiment",
    )
    _require_precedes(
        registry,
        "Experiment",
        experiment.record_id,
        "PromotionDecision",
        promotion.record_id,
        name="Experiment -> PromotionDecision",
    )

    model_artifact_sha256 = _sha256(
        model_payload.get("artifact_sha256"),
        "ModelVersion artifact_sha256",
    )
    artifact_hashes = evaluation.payload.get("artifact_hashes")
    if not isinstance(artifact_hashes, (list, tuple)):
        raise RegisteredStrategyModelRuntimeError(
            "EvaluationBundle artifact_hashes are invalid"
        )
    if model_artifact_sha256 not in artifact_hashes:
        raise RegisteredStrategyModelRuntimeError(
            "EvaluationBundle does not bind the promoted model artifact"
        )

    artifact_store = FactoryArtifactStore(
        workspace_path / FACTORY_ARTIFACT_DIRECTORY
    )
    try:
        artifact = artifact_store.read(
            "model",
            model_id,
            expected_sha256=model_artifact_sha256,
        )
    except (OSError, ValueError) as exc:
        raise RegisteredStrategyModelRuntimeError(
            "promoted model artifact is unavailable or hash-invalid"
        ) from exc
    artifact_training_manifest_sha256 = _sha256(
        artifact.get("training_points_manifest_sha256"),
        "model artifact training_points_manifest_sha256",
    )
    if artifact_training_manifest_sha256 != dataset_manifest_sha256:
        raise RegisteredStrategyModelRuntimeError(
            "model artifact training manifest does not match durable dataset"
        )
    artifact_evaluator_config_sha256 = _sha256(
        artifact.get("evaluator_config_sha256"),
        "model artifact evaluator_config_sha256",
    )
    if artifact_evaluator_config_sha256 != evaluation_config.config_sha256:
        raise RegisteredStrategyModelRuntimeError(
            "model artifact evaluator config does not match ResearchProtocol"
        )
    rebuilt = _decode_mean_baseline_artifact(
        artifact,
        model=model,
        strategy=strategy,
        experiment=experiment,
    )

    try:
        publication = artifact_store.publication_receipt(
            "model",
            model_id,
            expected_sha256=model_artifact_sha256,
        )
    except (OSError, ValueError) as exc:
        raise RegisteredStrategyModelRuntimeError(
            "promoted model lacks canonical factory publication authority"
        ) from exc
    publication_record_sha256 = _sha256(
        publication.get("record_sha256"),
        "factory publication record_sha256",
    )
    publication_final_registry_sha256 = _sha256(
        publication.get("final_registry_sha256"),
        "factory publication final_registry_sha256",
    )
    publication_committed_at = _text(
        publication.get("committed_at"),
        "factory publication committed_at",
    )
    if _instant(
        publication_committed_at,
        "factory publication committed_at",
    ) > decision_time:
        raise RegisteredStrategyModelRuntimeError(
            "promoted model artifact was published after runtime as_of"
        )
    publication_artifacts = publication.get("artifacts")
    if type(publication_artifacts) is not list:
        raise RegisteredStrategyModelRuntimeError(
            "factory publication artifact binding is invalid"
        )
    exact_publication_matches = [
        entry
        for entry in publication_artifacts
        if type(entry) is dict
        and entry.get("kind") == "model"
        and entry.get("identity") == model_id
        and entry.get("sha256") == model_artifact_sha256
    ]
    if len(exact_publication_matches) != 1:
        raise RegisteredStrategyModelRuntimeError(
            "factory publication does not bind the exact promoted model artifact"
        )
    if not publication_record_sha256:
        raise RegisteredStrategyModelRuntimeError(
            "factory publication authority is incomplete"
        )
    _require_publication_registry_prefix(
        registry,
        final_registry_sha256=publication_final_registry_sha256,
        model=model,
    )

    if _instant(rebuilt.training_cutoff, "model training_cutoff") > _instant(
        model.available_at,
        "ModelVersion available_at",
    ):
        raise RegisteredStrategyModelRuntimeError(
            "model training cutoff is after ModelVersion creation"
        )
    if _instant(rebuilt.training_cutoff, "model training_cutoff") > decision_time:
        raise RegisteredStrategyModelRuntimeError(
            "model training cutoff is after runtime as_of"
        )

    reproducibility = registry.reproducibility_bundle(experiment.record_id)
    references = reproducibility.get("references")
    if type(references) is not dict:
        raise RegisteredStrategyModelRuntimeError(
            "reproducibility bundle references are invalid"
        )
    for kind, expected_id, expected_sha in (
        ("StrategyVersion", strategy_id, strategy.record_sha256),
        ("ModelVersion", model_id, model.record_sha256),
        ("EvaluationBundle", evaluation_bundle_id, evaluation.record_sha256),
        ("FeatureSet", feature_set_id, feature.record_sha256),
        ("DatasetSnapshot", dataset_snapshot_id, dataset.record_sha256),
        ("ResearchProtocol", research_protocol_id, protocol.record_sha256),
    ):
        ref = references.get(kind)
        if (
            type(ref) is not dict
            or ref.get("id") != expected_id
            or ref.get("sha256") != expected_sha
        ):
            raise RegisteredStrategyModelRuntimeError(
                f"reproducibility bundle {kind} identity mismatch"
            )
    reproducibility_bundle_sha256 = _sha256(
        reproducibility.get("bundle_sha256"),
        "reproducibility bundle_sha256",
    )

    return RegisteredStrategyModelRuntime(
        strategy_version_id=strategy_id,
        strategy_record_sha256=strategy.record_sha256,
        model_version_id=model_id,
        model_record_sha256=model.record_sha256,
        model_artifact_sha256=model_artifact_sha256,
        model_identity_sha256=rebuilt.identity_sha256,
        promotion_record_sha256=promotion.record_sha256,
        experiment_id=experiment.record_id,
        reproducibility_bundle_sha256=reproducibility_bundle_sha256,
        canonical_strategy_id=canonical_strategy_id,
        research_protocol_id=research_protocol_id,
        dataset_snapshot_id=dataset_snapshot_id,
        feature_set_id=feature_set_id,
        authority_as_of=as_of,
        training_cutoff=rebuilt.training_cutoff,
        _model=rebuilt,
    )


resolve_registered_strategy_model = _bind_registered_strategy_runtime_resolver(
    resolve_registered_strategy_model
)
del _bind_registered_strategy_runtime_resolver


__all__ = [
    "FACTORY_ARTIFACT_DIRECTORY",
    "RegisteredStrategyModelRuntime",
    "RegisteredStrategyModelRuntimeError",
    "SCIENTIFIC_REGISTRY_FILE",
    "SUPPORTED_MODEL_FAMILY",
    "resolve_registered_strategy_model",
]
