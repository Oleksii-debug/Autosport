from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .scientific_registry import (
    EvaluationBundleRef,
    ExperimentRecord,
    ModelVersion,
    Postmortem,
    PromotionDecision,
    ResearchOutcome,
    ScientificRegistry,
    StrategyVersion,
)
from .strategy_model_factory import (
    DriftEvidence,
    DriftMonitor,
    PromotionController,
    PromotionEvaluation,
    PromotionRule,
    PromotionVerdict,
    TrainingPoint,
    WalkForwardResult,
    WalkForwardRunner,
)


def _instant(value: str, name: str) -> datetime:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _walk_forward_payload(result: WalkForwardResult) -> dict[str, object]:
    return {
        "model_family": result.model_family,
        "primary_metric": result.primary_metric,
        "primary_value": result.primary_value,
        "result_sha256": result.result_sha256,
        "folds": [
            {
                "fold_id": fold.fold_id,
                "training_cutoff": fold.training_cutoff,
                "evaluation_at": fold.evaluation_at,
                "prediction": fold.prediction,
                "target": fold.target,
                "squared_error": fold.squared_error,
            }
            for fold in result.folds
        ],
    }


def _drift_payload(evidence: Sequence[DriftEvidence]) -> list[dict[str, object]]:
    return [
        {
            "metric_name": item.metric_name,
            "reference_value": item.reference_value,
            "current_value": item.current_value,
            "absolute_threshold": item.absolute_threshold,
            "observed_at": item.observed_at,
            "drifted": item.drifted,
        }
        for item in evidence
    ]


@dataclass(frozen=True, slots=True)
class FactoryRegistryRunSpec:
    """Pre-frozen durable identities for one factory evaluation.

    Question, hypothesis, research protocol, dataset snapshot and feature set must
    already exist in the canonical ScientificRegistry. The candidate ModelVersion
    and StrategyVersion are appended before evaluation starts so final results
    cannot rewrite their identity or configuration after the fact.
    """

    model: ModelVersion
    strategy: StrategyVersion
    evaluation_bundle_id: str
    experiment_id: str
    promotion_decision_id: str
    started_at: str
    completed_at: str
    decided_at: str
    evaluator_source_sha256: str
    rejected_outcome: ResearchOutcome = ResearchOutcome.NEGATIVE
    postmortem_id: str | None = None
    retest_conditions: tuple[str, ...] = ("new frozen protocol or lawful dataset snapshot",)

    def __post_init__(self) -> None:
        if not isinstance(self.model, ModelVersion):
            raise ValueError("model must be a ModelVersion")
        if not isinstance(self.strategy, StrategyVersion):
            raise ValueError("strategy must be a StrategyVersion")
        if self.strategy.model_version_id != self.model.model_version_id:
            raise ValueError("strategy must bind the candidate model version")
        for name in (
            "evaluation_bundle_id",
            "experiment_id",
            "promotion_decision_id",
            "evaluator_source_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"{name} must be non-empty")
        started = _instant(self.started_at, "started_at")
        completed = _instant(self.completed_at, "completed_at")
        decided = _instant(self.decided_at, "decided_at")
        if completed < started:
            raise ValueError("completed_at must not precede started_at")
        if decided < completed:
            raise ValueError("decided_at must not precede completed_at")
        if not isinstance(self.rejected_outcome, ResearchOutcome):
            raise ValueError("rejected_outcome must be a ResearchOutcome")
        if self.rejected_outcome is ResearchOutcome.POSITIVE:
            raise ValueError("rejected_outcome cannot be POSITIVE")
        if self.postmortem_id is not None and not self.postmortem_id:
            raise ValueError("postmortem_id must be non-empty when supplied")
        if self.postmortem_id is not None and not self.retest_conditions:
            raise ValueError("retest_conditions are required for a rejection postmortem")


@dataclass(frozen=True, slots=True)
class FactoryRegistryResult:
    walk_forward: WalkForwardResult
    promotion: PromotionEvaluation
    evaluation_artifact_sha256: str
    experiment_fingerprint: str
    reproducibility_bundle_sha256: str
    drift_recommendations: tuple[str, ...]


class ScientificFactoryRegistryBridge:
    """Durable bridge from factory arithmetic into the canonical scientific registry.

    This owns no second registry, scheduler, market truth, stake arithmetic or
    execution authority. It freezes identities before evaluation, records both
    positive and rejected outcomes, and delegates promotion evidence validation to
    ScientificRegistry.record_promotion().
    """

    def __init__(self, registry: ScientificRegistry) -> None:
        if not isinstance(registry, ScientificRegistry):
            raise ValueError("registry must be a ScientificRegistry")
        self.registry = registry

    def _foundation(self, spec: FactoryRegistryRunSpec) -> tuple[dict[str, object], str]:
        started = _instant(spec.started_at, "started_at")

        def require(kind: str, identity: str):
            entry = self.registry.get(kind, identity)
            if entry is None:
                raise ValueError(f"factory prerequisite missing {kind}:{identity}")
            if _instant(entry.available_at, f"{kind}.available_at") > started:
                raise ValueError(f"factory prerequisite {kind}:{identity} was not frozen before evaluation")
            return entry

        protocol = require("ResearchProtocol", spec.model.research_protocol_id)
        dataset = require("DatasetSnapshot", spec.model.dataset_snapshot_id)
        feature = require("FeatureSet", spec.model.feature_set_id)
        binding = protocol.payload.get("binding")
        if type(binding) is not dict:
            raise ValueError("research protocol lacks a frozen scientific binding")
        if protocol.payload.get("dataset_manifest_sha256") != dataset.payload.get("manifest_sha256"):
            raise ValueError("dataset manifest does not match the frozen research protocol")
        if binding.get("causal_cutoff") != dataset.payload.get("causal_cutoff"):
            raise ValueError("dataset causal cutoff does not match the frozen research protocol")
        if binding.get("feature_set_version") != feature.payload.get("version"):
            raise ValueError("feature set version does not match the frozen research protocol")
        if binding.get("code_config_sha256") != spec.model.config_sha256.lower():
            raise ValueError("model config does not match the frozen research protocol")
        if spec.strategy.config_sha256.lower() != spec.model.config_sha256.lower():
            raise ValueError("strategy/model config identity mismatch")
        predecessor = spec.strategy.predecessor_strategy_version_id
        if predecessor is not None:
            previous = require("StrategyVersion", predecessor)
            if previous.payload.get("canonical_strategy_id") != spec.strategy.canonical_strategy_id:
                raise ValueError("candidate predecessor crosses strategy context")
        protocol_sha = protocol.payload.get("protocol_sha256")
        if type(protocol_sha) is not str:
            raise ValueError("research protocol lacks protocol_sha256")
        return binding, protocol_sha

    def run(
        self,
        spec: FactoryRegistryRunSpec,
        points: Sequence[TrainingPoint],
        *,
        promotion_rule: PromotionRule,
        champion_metrics: Mapping[str, float],
        challenger_protective_metrics: Mapping[str, float] | None = None,
        drift_evidence: Sequence[DriftEvidence] = (),
        minimum_train_size: int = 2,
    ) -> FactoryRegistryResult:
        binding, protocol_sha = self._foundation(spec)
        started = _instant(spec.started_at, "started_at")
        if _instant(spec.model.created_at, "ModelVersion.created_at") > started:
            raise ValueError("candidate model identity must be frozen before evaluation")
        if _instant(spec.strategy.created_at, "StrategyVersion.created_at") > started:
            raise ValueError("candidate strategy identity must be frozen before evaluation")

        # Persist immutable candidate identities before any final evaluation result exists.
        self.registry.append(spec.model)
        self.registry.append(spec.strategy)

        walk_forward = WalkForwardRunner.run(points, minimum_train_size=minimum_train_size)
        if promotion_rule.primary_metric != walk_forward.primary_metric:
            raise ValueError("frozen promotion primary metric does not match walk-forward result")

        challenger_metrics = dict(challenger_protective_metrics or {})
        if walk_forward.primary_metric in challenger_metrics:
            raise ValueError("challenger protective metrics must not override the primary metric")
        challenger_metrics[walk_forward.primary_metric] = walk_forward.primary_value
        promotion = PromotionController.evaluate(
            promotion_rule,
            champion_metrics=champion_metrics,
            challenger_metrics=challenger_metrics,
            provenance_complete=True,
            rollback_target=spec.strategy.predecessor_strategy_version_id,
        )
        recommendations = DriftMonitor.recommendations(drift_evidence)

        artifact_payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "autosport-strategy-model-factory-evaluation",
            "protocol_sha256": protocol_sha,
            "research_protocol_id": spec.model.research_protocol_id,
            "dataset_snapshot_id": spec.model.dataset_snapshot_id,
            "feature_set_id": spec.model.feature_set_id,
            "model_version_id": spec.model.model_version_id,
            "strategy_version_id": spec.strategy.strategy_version_id,
            "predecessor_strategy_version_id": spec.strategy.predecessor_strategy_version_id,
            "walk_forward": _walk_forward_payload(walk_forward),
            "promotion_rule": {
                "primary_metric": promotion_rule.primary_metric,
                "minimum_improvement": promotion_rule.minimum_improvement,
                "protective_metric_maxima": [list(item) for item in promotion_rule.protective_metric_maxima],
            },
            "champion_metrics": dict(sorted(champion_metrics.items())),
            "challenger_metrics": dict(sorted(challenger_metrics.items())),
            "promotion": {
                "verdict": promotion.verdict.value,
                "registry_action": promotion.registry_action.value,
                "primary_improvement": promotion.primary_improvement,
                "reasons": list(promotion.reasons),
            },
            "drift_evidence": _drift_payload(drift_evidence),
            "drift_recommendations": list(recommendations),
            "started_at": spec.started_at,
            "completed_at": spec.completed_at,
            "truth": {
                "real_money_execution": False,
                "automatic_financial_authority_expansion": False,
            },
        }
        artifact_sha = _digest(artifact_payload)
        bundle = EvaluationBundleRef(
            spec.evaluation_bundle_id,
            artifact_sha,
            spec.evaluator_source_sha256,
            spec.model.dataset_snapshot_id,
            protocol_sha,
            (walk_forward.result_sha256,),
            spec.completed_at,
            evaluated_strategy_version_id=spec.strategy.strategy_version_id,
            evaluated_model_version_id=spec.model.model_version_id,
        )
        self.registry.append(bundle)

        outcome = (
            ResearchOutcome.POSITIVE
            if promotion.verdict is PromotionVerdict.PROMOTE
            else spec.rejected_outcome
        )
        notes = _canonical_json(
            {
                "factory_evaluation_artifact": artifact_payload,
                "factory_evaluation_artifact_sha256": artifact_sha,
            }
        )
        experiment = ExperimentRecord(
            spec.experiment_id,
            spec.model.research_protocol_id,
            spec.model.dataset_snapshot_id,
            spec.model.feature_set_id,
            spec.strategy.strategy_version_id,
            spec.evaluation_bundle_id,
            spec.model.seed,
            spec.model.config_sha256,
            outcome,
            spec.started_at,
            model_version_id=spec.model.model_version_id,
            completed_at=spec.completed_at,
            notes=notes,
        )
        self.registry.append(experiment)

        decision = PromotionDecision(
            spec.promotion_decision_id,
            promotion.registry_action,
            spec.strategy.strategy_version_id,
            spec.model.research_protocol_id,
            protocol_sha,
            spec.evaluation_bundle_id,
            artifact_sha,
            spec.decided_at,
            predecessor_strategy_version_id=spec.strategy.predecessor_strategy_version_id,
            candidate_model_version_id=spec.model.model_version_id,
            reason="; ".join(promotion.reasons),
        )
        self.registry.record_promotion(decision)

        if promotion.verdict is PromotionVerdict.REJECT and spec.postmortem_id is not None:
            self.registry.append(
                Postmortem(
                    spec.postmortem_id,
                    spec.experiment_id,
                    outcome,
                    "; ".join(promotion.reasons),
                    spec.retest_conditions,
                    spec.decided_at,
                )
            )

        reproducibility = self.registry.reproducibility_bundle(spec.experiment_id)
        # The protocol binding was read before evaluation and is included in registry-level
        # promotion validation; keep this explicit read to make the freeze boundary evident.
        if binding.get("research_protocol_id") != spec.model.research_protocol_id:
            raise ValueError("model protocol identity does not match frozen binding")
        return FactoryRegistryResult(
            walk_forward=walk_forward,
            promotion=promotion,
            evaluation_artifact_sha256=artifact_sha,
            experiment_fingerprint=experiment.fingerprint,
            reproducibility_bundle_sha256=reproducibility["bundle_sha256"],
            drift_recommendations=recommendations,
        )
