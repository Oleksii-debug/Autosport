from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence

from .integrity import atomic_write_json, sha256_file
from .scientific_registry import (
    EvaluationBundleRef,
    ExperimentRecord,
    ModelVersion,
    Postmortem,
    PromotionAction,
    PromotionDecision,
    ResearchOutcome,
    ScientificRegistry,
    StrategyVersion,
)


def _canonical_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite(value: object, name: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _metric_map(value: object, name: str) -> dict[str, float]:
    if type(value) is not dict or not value:
        raise ValueError(f"{name} must be a non-empty metric object")
    result: dict[str, float] = {}
    for key, metric in value.items():
        key = _text(key, f"{name} metric name")
        if key in result:
            raise ValueError(f"{name} metric names must be unique")
        result[key] = _finite(metric, f"{name}.{key}")
    return dict(sorted(result.items()))


@dataclass(frozen=True, slots=True)
class TrainingPoint:
    observed_at: str
    feature: float
    target: float
    target_available_at: str | None = None

    def __post_init__(self) -> None:
        _instant(self.observed_at, "observed_at")
        _finite(self.feature, "feature")
        _finite(self.target, "target")
        if self.target_available_at is not None:
            _instant(self.target_available_at, "target_available_at")

    @property
    def target_reveal_at(self) -> str:
        return self.target_available_at or self.observed_at


@dataclass(frozen=True, slots=True)
class MeanBaselineModel:
    """Transparent deterministic baseline used before more complex model adapters."""

    model_id: str
    training_cutoff: str
    mean_target: float
    training_count: int

    @classmethod
    def fit(
        cls,
        model_id: str,
        points: Sequence[TrainingPoint],
        *,
        training_cutoff: str,
    ) -> "MeanBaselineModel":
        _text(model_id, "model_id")
        cutoff = _instant(training_cutoff, "training_cutoff")
        eligible = tuple(
            point
            for point in points
            if _instant(point.observed_at, "observed_at") <= cutoff
            and _instant(point.target_reveal_at, "target_available_at") <= cutoff
        )
        if not eligible:
            raise ValueError("baseline requires causal training observations with revealed targets")
        mean = sum(_finite(point.target, "target") for point in eligible) / len(eligible)
        return cls(model_id, training_cutoff, mean, len(eligible))

    def predict(self, point: TrainingPoint, *, decision_at: str) -> float:
        decision = _instant(decision_at, "decision_at")
        if _instant(point.observed_at, "observed_at") > decision:
            raise ValueError("prediction input is not available at decision time")
        if _instant(self.training_cutoff, "training_cutoff") > decision:
            raise ValueError("model training cutoff exceeds decision time")
        return self.mean_target

    @property
    def identity_sha256(self) -> str:
        return _canonical_digest(
            {
                "family": "mean-baseline-v1",
                "model_id": self.model_id,
                "training_cutoff": self.training_cutoff,
                "mean_target": self.mean_target,
                "training_count": self.training_count,
            }
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "family": "mean-baseline-v1",
            "model_id": self.model_id,
            "training_cutoff": self.training_cutoff,
            "mean_target": self.mean_target,
            "training_count": self.training_count,
            "identity_sha256": self.identity_sha256,
        }


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_id: str
    training_cutoff: str
    evaluation_at: str
    target_available_at: str
    prediction: float
    target: float
    squared_error: float


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    model_family: str
    folds: tuple[WalkForwardFold, ...]
    primary_metric: str
    primary_value: float

    def to_payload(self) -> dict[str, object]:
        return {
            "model_family": self.model_family,
            "primary_metric": self.primary_metric,
            "primary_value": self.primary_value,
            "folds": [
                {
                    "fold_id": fold.fold_id,
                    "training_cutoff": fold.training_cutoff,
                    "evaluation_at": fold.evaluation_at,
                    "target_available_at": fold.target_available_at,
                    "prediction": fold.prediction,
                    "target": fold.target,
                    "squared_error": fold.squared_error,
                }
                for fold in self.folds
            ],
        }

    @property
    def result_sha256(self) -> str:
        return _canonical_digest(self.to_payload())


class WalkForwardRunner:
    """Causal expanding-window evaluator; evaluation labels never enter their own training set."""

    @staticmethod
    def run(points: Sequence[TrainingPoint], *, minimum_train_size: int = 2) -> WalkForwardResult:
        if type(minimum_train_size) is not int or minimum_train_size < 1:
            raise ValueError("minimum_train_size must be a positive integer")
        ordered = tuple(
            sorted(points, key=lambda point: _instant(point.observed_at, "observed_at"))
        )
        if len(ordered) <= minimum_train_size:
            raise ValueError("not enough observations for walk-forward evaluation")
        instants = tuple(_instant(point.observed_at, "observed_at") for point in ordered)
        if len(set(instants)) != len(instants):
            raise ValueError("walk-forward observations require unique timestamps")

        folds: list[WalkForwardFold] = []
        for index in range(minimum_train_size, len(ordered)):
            evaluation = ordered[index]
            train = ordered[:index]
            cutoff = train[-1].observed_at
            if _instant(cutoff, "training_cutoff") >= _instant(
                evaluation.observed_at, "evaluation_at"
            ):
                raise ValueError("walk-forward cutoff must precede evaluation")
            model = MeanBaselineModel.fit(
                f"mean-baseline-fold-{index}", train, training_cutoff=cutoff
            )
            prediction = model.predict(evaluation, decision_at=evaluation.observed_at)
            target = _finite(evaluation.target, "target")
            squared_error = (prediction - target) ** 2
            folds.append(
                WalkForwardFold(
                    fold_id=f"fold-{index}",
                    training_cutoff=cutoff,
                    evaluation_at=evaluation.observed_at,
                    target_available_at=evaluation.target_reveal_at,
                    prediction=prediction,
                    target=target,
                    squared_error=squared_error,
                )
            )
        mse = sum(fold.squared_error for fold in folds) / len(folds)
        return WalkForwardResult("mean-baseline-v1", tuple(folds), "mse", mse)


class PromotionVerdict(StrEnum):
    PROMOTE = "PROMOTE"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class PromotionRule:
    primary_metric: str
    minimum_improvement: float
    protective_metric_maxima: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        _text(self.primary_metric, "primary_metric")
        improvement = _finite(self.minimum_improvement, "minimum_improvement")
        if improvement < 0:
            raise ValueError("minimum_improvement must be non-negative")
        names: set[str] = set()
        for name, maximum in self.protective_metric_maxima:
            _text(name, "protective metric name")
            if name in names:
                raise ValueError("protective metric names must be unique and non-empty")
            names.add(name)
            _finite(maximum, f"protective maximum {name}")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "kind": "autosport-promotion-rule-v1",
            "primary_metric": self.primary_metric,
            "minimum_improvement": _finite(
                self.minimum_improvement, "minimum_improvement"
            ),
            "protective_metric_maxima": [
                [name, _finite(maximum, f"protective maximum {name}")]
                for name, maximum in sorted(self.protective_metric_maxima)
            ],
            "metric_direction": "lower_is_better",
        }

    @property
    def frozen_text(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @property
    def rule_sha256(self) -> str:
        return _canonical_digest(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class PromotionEvaluation:
    verdict: PromotionVerdict
    registry_action: PromotionAction
    primary_improvement: float
    reasons: tuple[str, ...]


class PromotionController:
    """Deterministic fail-closed comparison; it owns no execution authority."""

    @staticmethod
    def evaluate(
        rule: PromotionRule,
        *,
        champion_metrics: Mapping[str, float],
        challenger_metrics: Mapping[str, float],
        provenance_complete: bool,
        rollback_target: str | None,
    ) -> PromotionEvaluation:
        if provenance_complete is not True:
            return PromotionEvaluation(
                PromotionVerdict.REJECT,
                PromotionAction.REJECT,
                0.0,
                ("incomplete provenance",),
            )
        if type(rollback_target) is not str or not rollback_target:
            return PromotionEvaluation(
                PromotionVerdict.REJECT,
                PromotionAction.REJECT,
                0.0,
                ("missing rollback lineage",),
            )
        if rule.primary_metric not in champion_metrics or rule.primary_metric not in challenger_metrics:
            return PromotionEvaluation(
                PromotionVerdict.REJECT,
                PromotionAction.REJECT,
                0.0,
                ("missing primary metric",),
            )
        champion = _finite(champion_metrics[rule.primary_metric], "champion primary metric")
        challenger = _finite(
            challenger_metrics[rule.primary_metric], "challenger primary metric"
        )
        improvement = champion - challenger
        reasons: list[str] = []
        if improvement < rule.minimum_improvement:
            reasons.append("primary improvement below frozen threshold")
        for name, maximum in rule.protective_metric_maxima:
            if name not in challenger_metrics:
                reasons.append(f"missing protective metric: {name}")
                continue
            if _finite(
                challenger_metrics[name], f"challenger protective metric {name}"
            ) > maximum:
                reasons.append(f"protective metric degraded: {name}")
        if reasons:
            return PromotionEvaluation(
                PromotionVerdict.REJECT,
                PromotionAction.REJECT,
                improvement,
                tuple(reasons),
            )
        return PromotionEvaluation(
            PromotionVerdict.PROMOTE,
            PromotionAction.PROMOTE,
            improvement,
            ("frozen promotion rule satisfied",),
        )


class CandidateStudyAdapter(Protocol):
    """Optional HPO seam; a future approved Optuna adapter may implement it."""

    def candidate_configs(
        self, *, study_id: str, frozen_config_sha256: str
    ) -> tuple[Mapping[str, object], ...]: ...


class FactoryArtifactStore:
    """Immutable deterministic artifacts referenced by canonical ScientificRegistry hashes."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _filename(kind: str, identity: str) -> str:
        _text(kind, "kind")
        identity = _text(identity, "identity")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{kind}-{digest}.json"

    def _path(self, kind: str, identity: str) -> Path:
        return self.root / self._filename(kind, identity)

    def write(self, kind: str, identity: str, payload: dict[str, object]) -> str:
        path = self._path(kind, identity)
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("existing factory artifact is invalid JSON") from exc
            if existing != payload:
                raise ValueError(f"conflicting immutable factory artifact: {kind}:{identity}")
            return sha256_file(path)
        atomic_write_json(path, payload)
        return sha256_file(path)

    def read(
        self, kind: str, identity: str, *, expected_sha256: str | None = None
    ) -> dict[str, object]:
        path = self._path(kind, identity)
        if not path.is_file():
            raise ValueError(f"factory artifact is missing: {kind}:{identity}")
        if expected_sha256 is not None and sha256_file(path) != _sha256(
            expected_sha256, "expected_sha256"
        ):
            raise ValueError(f"factory artifact hash mismatch: {kind}:{identity}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("factory artifact is invalid JSON") from exc
        if type(payload) is not dict:
            raise ValueError("factory artifact root must be an object")
        return payload

    def sha256(self, kind: str, identity: str) -> str:
        path = self._path(kind, identity)
        if not path.is_file():
            raise ValueError(f"factory artifact is missing: {kind}:{identity}")
        return sha256_file(path)

    def path_for_testing(self, kind: str, identity: str) -> Path:
        return self._path(kind, identity)


@dataclass(frozen=True, slots=True)
class FactoryCandidateSpec:
    experiment_id: str
    model_version_id: str
    strategy_version_id: str
    evaluation_bundle_id: str
    promotion_decision_id: str
    canonical_strategy_id: str
    research_protocol_id: str
    dataset_snapshot_id: str
    feature_set_id: str
    source_sha256: str
    environment_sha256: str
    evaluator_source_sha256: str
    seed: int
    created_at: str
    completed_at: str
    decided_at: str
    predecessor_strategy_version_id: str | None
    predecessor_model_version_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "experiment_id",
            "model_version_id",
            "strategy_version_id",
            "evaluation_bundle_id",
            "promotion_decision_id",
            "canonical_strategy_id",
            "research_protocol_id",
            "dataset_snapshot_id",
            "feature_set_id",
        ):
            _text(getattr(self, name), name)
        for name in ("source_sha256", "environment_sha256", "evaluator_source_sha256"):
            _sha256(getattr(self, name), name)
        if type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        created = _instant(self.created_at, "created_at")
        completed = _instant(self.completed_at, "completed_at")
        decided = _instant(self.decided_at, "decided_at")
        if completed < created:
            raise ValueError("completed_at must not precede created_at")
        if decided < completed:
            raise ValueError("decided_at must not precede completed_at")
        for name in ("predecessor_strategy_version_id", "predecessor_model_version_id"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)


@dataclass(frozen=True, slots=True)
class FactoryRunResult:
    experiment_id: str
    model_version_id: str
    strategy_version_id: str
    evaluation_bundle_id: str
    promotion_decision_id: str
    evaluation_bundle_sha256: str
    reproducibility_bundle_sha256: str
    verdict: PromotionVerdict
    registry_action: PromotionAction
    candidate_metrics: dict[str, float]


@dataclass(frozen=True, slots=True)
class FactoryRestartEvidence:
    experiment_id: str
    experiment_fingerprint: str
    evaluation_bundle_sha256: str
    model_artifact_sha256: str
    reproducibility_bundle_sha256: str
    champion_strategy_version_id: str | None
    outcome: ResearchOutcome


class ExperimentRunner:
    """Executable factory vertical backed by the canonical ScientificRegistry."""

    def __init__(
        self, registry: ScientificRegistry, artifact_store: FactoryArtifactStore
    ) -> None:
        if not isinstance(registry, ScientificRegistry):
            raise ValueError("registry must be ScientificRegistry")
        self.registry = registry
        self.artifact_store = artifact_store

    def _foundation(
        self, spec: FactoryCandidateSpec, rule: PromotionRule
    ) -> tuple[dict[str, object], str, str]:
        protocol = self.registry.get("ResearchProtocol", spec.research_protocol_id)
        dataset = self.registry.get("DatasetSnapshot", spec.dataset_snapshot_id)
        feature = self.registry.get("FeatureSet", spec.feature_set_id)
        if protocol is None or dataset is None or feature is None:
            raise ValueError("factory foundation is incomplete in ScientificRegistry")
        binding = protocol.payload.get("binding")
        if type(binding) is not dict:
            raise ValueError("research protocol lacks frozen binding")
        if binding.get("promotion_rule") != rule.frozen_text:
            raise ValueError("promotion rule does not match frozen research protocol")
        hypothesis_id = binding.get("hypothesis_id")
        if type(hypothesis_id) is not str:
            raise ValueError("research protocol lacks frozen hypothesis identity")
        hypothesis = self.registry.get("Hypothesis", hypothesis_id)
        if hypothesis is None:
            raise ValueError("frozen hypothesis is missing")
        if hypothesis.payload.get("primary_metric") != rule.primary_metric:
            raise ValueError("promotion primary metric does not match frozen hypothesis")
        protective = hypothesis.payload.get("protective_metrics")
        if type(protective) is not list:
            raise ValueError("frozen hypothesis protective metrics are invalid")
        rule_protective = {name for name, _ in rule.protective_metric_maxima}
        if not rule_protective.issubset(set(protective)):
            raise ValueError("promotion protective metrics are not frozen in hypothesis")
        if dataset.payload.get("manifest_sha256") != protocol.payload.get(
            "dataset_manifest_sha256"
        ):
            raise ValueError("dataset manifest does not match frozen research protocol")
        if feature.payload.get("version") != binding.get("feature_set_version"):
            raise ValueError("feature set version does not match frozen research protocol")
        causal_cutoff = binding.get("causal_cutoff")
        if type(causal_cutoff) is not str:
            raise ValueError("research protocol lacks causal cutoff")
        if _instant(dataset.payload.get("causal_cutoff"), "dataset causal_cutoff") != _instant(
            causal_cutoff, "protocol causal_cutoff"
        ):
            raise ValueError("dataset causal cutoff does not match frozen research protocol")
        config_sha256 = _sha256(binding.get("code_config_sha256"), "code_config_sha256")
        return binding, config_sha256, protocol.payload["protocol_sha256"]

    def _durable_champion_metrics(
        self,
        *,
        champion_strategy_version_id: str,
        champion_evaluation_bundle_id: str,
    ) -> dict[str, float]:
        strategy = self.registry.get("StrategyVersion", champion_strategy_version_id)
        bundle = self.registry.get("EvaluationBundle", champion_evaluation_bundle_id)
        if strategy is None or bundle is None:
            raise ValueError("durable champion evaluation provenance is missing")
        champion_model_version_id = strategy.payload.get("model_version_id")
        if type(champion_model_version_id) is not str:
            raise ValueError("durable champion model identity is missing")
        if bundle.payload.get("evaluated_strategy_version_id") != champion_strategy_version_id:
            raise ValueError("champion evaluation does not reference durable champion strategy")
        if bundle.payload.get("evaluated_model_version_id") != champion_model_version_id:
            raise ValueError("champion evaluation does not reference durable champion model")

        metrics_artifact_sha256 = self.artifact_store.sha256(
            "metrics", champion_evaluation_bundle_id
        )
        artifact_hashes = bundle.payload.get("artifact_hashes")
        if type(artifact_hashes) is not list or metrics_artifact_sha256 not in artifact_hashes:
            raise ValueError("champion metrics artifact is not hash-bound to EvaluationBundle")
        payload = self.artifact_store.read(
            "metrics",
            champion_evaluation_bundle_id,
            expected_sha256=metrics_artifact_sha256,
        )
        if payload.get("kind") != "autosport-factory-metrics-v1":
            raise ValueError("champion metrics artifact kind mismatch")
        if payload.get("evaluation_bundle_id") != champion_evaluation_bundle_id:
            raise ValueError("champion metrics evaluation identity mismatch")
        if payload.get("strategy_version_id") != champion_strategy_version_id:
            raise ValueError("champion metrics strategy identity mismatch")
        if payload.get("model_version_id") != champion_model_version_id:
            raise ValueError("champion metrics model identity mismatch")
        return _metric_map(payload.get("metrics"), "champion metrics")

    def run_baseline_candidate(
        self,
        spec: FactoryCandidateSpec,
        points: Sequence[TrainingPoint],
        *,
        rule: PromotionRule,
        champion_evaluation_bundle_id: str,
        protective_metrics: Mapping[str, float],
        minimum_train_size: int = 2,
    ) -> FactoryRunResult:
        binding, config_sha256, protocol_sha256 = self._foundation(spec, rule)
        current_champion = self.registry.champion_strategy(
            as_of=spec.decided_at,
            canonical_strategy_id=spec.canonical_strategy_id,
        )
        if current_champion != spec.predecessor_strategy_version_id:
            raise ValueError("candidate predecessor does not match durable context champion")
        if current_champion is None:
            raise ValueError("factory challenger promotion requires a durable rollback champion")
        champion_metrics = self._durable_champion_metrics(
            champion_strategy_version_id=current_champion,
            champion_evaluation_bundle_id=champion_evaluation_bundle_id,
        )

        walk_forward = WalkForwardRunner.run(points, minimum_train_size=minimum_train_size)
        if walk_forward.primary_metric != rule.primary_metric:
            raise ValueError("walk-forward primary metric does not match frozen promotion rule")
        completed = _instant(spec.completed_at, "completed_at")
        if any(
            _instant(fold.target_available_at, "target_available_at") > completed
            for fold in walk_forward.folds
        ):
            raise ValueError("evaluation target was not revealed by experiment completion")

        final_model = MeanBaselineModel.fit(
            spec.model_version_id,
            points,
            training_cutoff=binding["causal_cutoff"],
        )
        candidate_metrics = {rule.primary_metric: walk_forward.primary_value}
        for name, value in protective_metrics.items():
            _text(name, "protective metric name")
            if name in candidate_metrics:
                raise ValueError("protective metrics must not overwrite primary metric")
            candidate_metrics[name] = _finite(value, f"protective metric {name}")
        candidate_metrics = dict(sorted(candidate_metrics.items()))
        promotion = PromotionController.evaluate(
            rule,
            champion_metrics=champion_metrics,
            challenger_metrics=candidate_metrics,
            provenance_complete=True,
            rollback_target=current_champion,
        )

        model_payload = final_model.to_payload()
        model_payload.update(
            {
                "model_version_id": spec.model_version_id,
                "research_protocol_id": spec.research_protocol_id,
                "dataset_snapshot_id": spec.dataset_snapshot_id,
                "feature_set_id": spec.feature_set_id,
                "config_sha256": config_sha256,
                "seed": spec.seed,
            }
        )
        model_artifact_sha256 = self.artifact_store.write(
            "model", spec.model_version_id, model_payload
        )
        self.registry.append(
            ModelVersion(
                spec.model_version_id,
                "mean-baseline-v1",
                model_artifact_sha256,
                spec.source_sha256,
                spec.environment_sha256,
                spec.dataset_snapshot_id,
                spec.feature_set_id,
                spec.research_protocol_id,
                spec.seed,
                config_sha256,
                spec.created_at,
                predecessor_model_version_id=spec.predecessor_model_version_id,
            )
        )

        self.registry.append(
            StrategyVersion(
                spec.strategy_version_id,
                spec.canonical_strategy_id,
                spec.source_sha256,
                spec.environment_sha256,
                config_sha256,
                spec.created_at,
                model_version_id=spec.model_version_id,
                predecessor_strategy_version_id=spec.predecessor_strategy_version_id,
            )
        )

        metrics_payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "autosport-factory-metrics-v1",
            "evaluation_bundle_id": spec.evaluation_bundle_id,
            "strategy_version_id": spec.strategy_version_id,
            "model_version_id": spec.model_version_id,
            "metrics": candidate_metrics,
        }
        candidate_metrics_artifact_sha256 = self.artifact_store.write(
            "metrics", spec.evaluation_bundle_id, metrics_payload
        )

        evaluation_payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "autosport-strategy-model-factory-evaluation",
            "evaluation_bundle_id": spec.evaluation_bundle_id,
            "experiment_id": spec.experiment_id,
            "research_protocol_id": spec.research_protocol_id,
            "protocol_sha256": protocol_sha256,
            "promotion_rule_sha256": rule.rule_sha256,
            "dataset_snapshot_id": spec.dataset_snapshot_id,
            "feature_set_id": spec.feature_set_id,
            "model_version_id": spec.model_version_id,
            "strategy_version_id": spec.strategy_version_id,
            "seed": spec.seed,
            "config_sha256": config_sha256,
            "walk_forward": walk_forward.to_payload(),
            "candidate_metrics": candidate_metrics,
            "candidate_metrics_artifact_sha256": candidate_metrics_artifact_sha256,
            "champion_evaluation_bundle_id": champion_evaluation_bundle_id,
            "champion_metrics": champion_metrics,
            "promotion_verdict": promotion.verdict.value,
            "promotion_reasons": list(promotion.reasons),
            "completed_at": spec.completed_at,
            "decided_at": spec.decided_at,
            "truth": {
                "real_money_execution": False,
                "auto_execution_authority": False,
                "llm_arithmetic_authority": False,
            },
        }
        evaluation_bundle_sha256 = self.artifact_store.write(
            "evaluation", spec.evaluation_bundle_id, evaluation_payload
        )
        self.registry.append(
            EvaluationBundleRef(
                spec.evaluation_bundle_id,
                evaluation_bundle_sha256,
                spec.evaluator_source_sha256,
                spec.dataset_snapshot_id,
                protocol_sha256,
                (model_artifact_sha256, candidate_metrics_artifact_sha256),
                spec.completed_at,
                evaluated_strategy_version_id=spec.strategy_version_id,
                evaluated_model_version_id=spec.model_version_id,
            )
        )

        outcome = (
            ResearchOutcome.POSITIVE
            if promotion.verdict is PromotionVerdict.PROMOTE
            else ResearchOutcome.NEGATIVE
        )
        experiment = ExperimentRecord(
            spec.experiment_id,
            spec.research_protocol_id,
            spec.dataset_snapshot_id,
            spec.feature_set_id,
            spec.strategy_version_id,
            spec.evaluation_bundle_id,
            spec.seed,
            config_sha256,
            outcome,
            spec.created_at,
            model_version_id=spec.model_version_id,
            completed_at=spec.completed_at,
            notes="; ".join(promotion.reasons),
        )
        self.registry.append(experiment)

        self.registry.record_promotion(
            PromotionDecision(
                spec.promotion_decision_id,
                promotion.registry_action,
                spec.strategy_version_id,
                spec.research_protocol_id,
                protocol_sha256,
                spec.evaluation_bundle_id,
                evaluation_bundle_sha256,
                spec.decided_at,
                predecessor_strategy_version_id=spec.predecessor_strategy_version_id,
                candidate_model_version_id=spec.model_version_id,
                reason="; ".join(promotion.reasons),
            )
        )

        if outcome is not ResearchOutcome.POSITIVE:
            self.registry.append(
                Postmortem(
                    f"{spec.experiment_id}:postmortem",
                    spec.experiment_id,
                    outcome,
                    "; ".join(promotion.reasons)
                    or "frozen promotion rule rejected candidate",
                    ("new protocol version or explicitly authorized retest",),
                    spec.decided_at,
                )
            )

        reproducibility = self.registry.reproducibility_bundle(spec.experiment_id)
        return FactoryRunResult(
            spec.experiment_id,
            spec.model_version_id,
            spec.strategy_version_id,
            spec.evaluation_bundle_id,
            spec.promotion_decision_id,
            evaluation_bundle_sha256,
            reproducibility["bundle_sha256"],
            promotion.verdict,
            promotion.registry_action,
            candidate_metrics,
        )

    @staticmethod
    def verify_restart(
        registry_path: str | Path,
        artifact_root: str | Path,
        experiment_id: str,
        *,
        as_of: str,
    ) -> FactoryRestartEvidence:
        registry = ScientificRegistry(registry_path)
        experiment = registry.get("Experiment", experiment_id)
        if experiment is None:
            raise ValueError("experiment is missing after restart")
        evaluation_bundle_id = experiment.payload.get("evaluation_bundle_id")
        model_version_id = experiment.payload.get("model_version_id")
        strategy_version_id = experiment.payload.get("strategy_version_id")
        if not all(
            isinstance(value, str)
            for value in (evaluation_bundle_id, model_version_id, strategy_version_id)
        ):
            raise ValueError("experiment lineage is incomplete after restart")
        bundle = registry.get("EvaluationBundle", evaluation_bundle_id)
        model = registry.get("ModelVersion", model_version_id)
        strategy = registry.get("StrategyVersion", strategy_version_id)
        if bundle is None or model is None or strategy is None:
            raise ValueError("factory lineage is incomplete after restart")
        store = FactoryArtifactStore(artifact_root)
        evaluation_payload = store.read(
            "evaluation",
            evaluation_bundle_id,
            expected_sha256=bundle.payload["bundle_sha256"],
        )
        store.read(
            "model",
            model_version_id,
            expected_sha256=model.payload["artifact_sha256"],
        )
        metrics_sha256 = evaluation_payload.get("candidate_metrics_artifact_sha256")
        if type(metrics_sha256) is not str:
            raise ValueError("evaluation artifact lacks metrics provenance")
        if metrics_sha256 not in bundle.payload.get("artifact_hashes", []):
            raise ValueError("metrics artifact is not hash-bound to EvaluationBundle")
        metrics_payload = store.read(
            "metrics", evaluation_bundle_id, expected_sha256=metrics_sha256
        )
        if metrics_payload.get("strategy_version_id") != strategy_version_id:
            raise ValueError("metrics artifact strategy identity mismatch")
        if metrics_payload.get("model_version_id") != model_version_id:
            raise ValueError("metrics artifact model identity mismatch")
        if evaluation_payload.get("experiment_id") != experiment_id:
            raise ValueError("evaluation artifact experiment identity mismatch")
        if evaluation_payload.get("model_version_id") != model_version_id:
            raise ValueError("evaluation artifact model identity mismatch")
        if evaluation_payload.get("strategy_version_id") != strategy_version_id:
            raise ValueError("evaluation artifact strategy identity mismatch")
        reproducibility = registry.reproducibility_bundle(experiment_id)
        canonical_strategy_id = strategy.payload.get("canonical_strategy_id")
        if type(canonical_strategy_id) is not str:
            raise ValueError("strategy context identity is missing")
        champion = registry.champion_strategy(
            as_of=as_of, canonical_strategy_id=canonical_strategy_id
        )
        return FactoryRestartEvidence(
            experiment_id,
            experiment.payload["fingerprint"],
            bundle.payload["bundle_sha256"],
            model.payload["artifact_sha256"],
            reproducibility["bundle_sha256"],
            champion,
            ResearchOutcome(experiment.payload["outcome"]),
        )


@dataclass(frozen=True, slots=True)
class DriftEvidence:
    metric_name: str
    reference_value: float
    current_value: float
    absolute_threshold: float
    observed_at: str

    def __post_init__(self) -> None:
        _text(self.metric_name, "metric_name")
        _instant(self.observed_at, "observed_at")

    @property
    def drifted(self) -> bool:
        reference = _finite(self.reference_value, "reference_value")
        current = _finite(self.current_value, "current_value")
        threshold = _finite(self.absolute_threshold, "absolute_threshold")
        if threshold < 0:
            raise ValueError("absolute_threshold must be non-negative")
        return abs(current - reference) > threshold

    def to_payload(self) -> dict[str, object]:
        return {
            "metric_name": self.metric_name,
            "reference_value": _finite(self.reference_value, "reference_value"),
            "current_value": _finite(self.current_value, "current_value"),
            "absolute_threshold": _finite(
                self.absolute_threshold, "absolute_threshold"
            ),
            "observed_at": self.observed_at,
            "drifted": self.drifted,
        }


@dataclass(frozen=True, slots=True)
class DriftRecord:
    evaluation_bundle_id: str
    bundle_sha256: str
    recommendations: tuple[str, ...]


class DriftMonitor:
    """Evidence-only drift monitor; it can recommend research, never promote a model."""

    @staticmethod
    def recommendations(evidence: Iterable[DriftEvidence]) -> tuple[str, ...]:
        recommendations = []
        for item in evidence:
            if item.drifted:
                recommendations.append(
                    f"RESEARCH_CHALLENGER:{item.metric_name}:{item.observed_at}"
                )
        return tuple(recommendations)

    @staticmethod
    def record_evidence(
        registry: ScientificRegistry,
        artifact_store: FactoryArtifactStore,
        *,
        evaluation_bundle_id: str,
        strategy_version_id: str,
        model_version_id: str,
        dataset_snapshot_id: str,
        research_protocol_id: str,
        evaluator_source_sha256: str,
        evidence: Sequence[DriftEvidence],
        recorded_at: str,
    ) -> DriftRecord:
        _text(evaluation_bundle_id, "evaluation_bundle_id")
        _sha256(evaluator_source_sha256, "evaluator_source_sha256")
        recorded = _instant(recorded_at, "recorded_at")
        if not evidence:
            raise ValueError("drift evidence must not be empty")
        if any(_instant(item.observed_at, "observed_at") > recorded for item in evidence):
            raise ValueError("drift evidence is not causally available at record time")

        protocol = registry.get("ResearchProtocol", research_protocol_id)
        dataset = registry.get("DatasetSnapshot", dataset_snapshot_id)
        strategy = registry.get("StrategyVersion", strategy_version_id)
        model = registry.get("ModelVersion", model_version_id)
        if protocol is None or dataset is None or strategy is None or model is None:
            raise ValueError("drift lineage is incomplete in ScientificRegistry")
        if strategy.payload.get("model_version_id") != model_version_id:
            raise ValueError("drift strategy/model lineage mismatch")
        if model.payload.get("dataset_snapshot_id") != dataset_snapshot_id:
            raise ValueError("drift model/dataset lineage mismatch")
        if model.payload.get("research_protocol_id") != research_protocol_id:
            raise ValueError("drift model/protocol lineage mismatch")

        recommendations = DriftMonitor.recommendations(evidence)
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "autosport-strategy-model-factory-drift-evidence",
            "evaluation_bundle_id": evaluation_bundle_id,
            "strategy_version_id": strategy_version_id,
            "model_version_id": model_version_id,
            "dataset_snapshot_id": dataset_snapshot_id,
            "research_protocol_id": research_protocol_id,
            "protocol_sha256": protocol.payload["protocol_sha256"],
            "evidence": [item.to_payload() for item in evidence],
            "recommendations": list(recommendations),
            "recorded_at": recorded_at,
            "authority_effect": "NONE",
            "truth": {
                "auto_promotion": False,
                "real_money_execution": False,
            },
        }
        bundle_sha256 = artifact_store.write("drift", evaluation_bundle_id, payload)
        registry.append(
            EvaluationBundleRef(
                evaluation_bundle_id,
                bundle_sha256,
                evaluator_source_sha256,
                dataset_snapshot_id,
                protocol.payload["protocol_sha256"],
                (bundle_sha256,),
                recorded_at,
                evaluated_strategy_version_id=strategy_version_id,
                evaluated_model_version_id=model_version_id,
            )
        )
        return DriftRecord(evaluation_bundle_id, bundle_sha256, recommendations)
