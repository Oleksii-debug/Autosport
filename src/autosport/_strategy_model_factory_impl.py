from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from decimal import Decimal
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence

from .integrity import atomic_write_json, sha256_file
from .scientific_registry import (
    DuplicateExperimentFingerprintError,
    EvaluationBundleRef,
    ExperimentRecord,
    ModelVersion,
    Postmortem,
    PromotionAction,
    PromotionDecision,
    PromotionEvidence,
    PromotionEvidenceDirection,
    PromotionEvidenceValidity,
    promotion_holdout_access_id,
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


def _canonical_instant(value: object, name: str) -> str:
    return _instant(value, name).isoformat()


def _promotion_effect_interval(
    paired_deltas: Sequence[Decimal],
    declared_uncertainty_method: object,
) -> tuple[str, Decimal, Decimal]:
    """Return an interval only when the frozen protocol names this exact procedure."""
    method = _text(declared_uncertainty_method, "uncertainty_method")
    if method != "paired min/max interval":
        raise ValueError(
            "unsupported frozen uncertainty_method for deterministic paired-fold "
            "interval; protocol must explicitly declare 'paired min/max interval'"
        )
    if not paired_deltas:
        raise ValueError("promotion evidence requires at least one paired causal holdout fold")
    if any(not isinstance(delta, Decimal) or not delta.is_finite() for delta in paired_deltas):
        raise ValueError("paired deltas must be finite Decimal values")
    ordered = sorted(paired_deltas)
    return method, ordered[0], ordered[-1]


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
        observed = _instant(self.observed_at, "observed_at")
        _finite(self.feature, "feature")
        _finite(self.target, "target")
        if self.target_available_at is not None:
            revealed = _instant(self.target_available_at, "target_available_at")
            if revealed < observed:
                raise ValueError("target_available_at must not precede observed_at")

    @property
    def target_reveal_at(self) -> str:
        if self.target_available_at is None:
            raise ValueError(
                "target_available_at is required for causal supervised labels"
            )
        return self.target_available_at


def _ordered_training_points(points: Sequence[TrainingPoint]) -> tuple[TrainingPoint, ...]:
    for point in points:
        if not isinstance(point, TrainingPoint):
            raise ValueError("training points must be TrainingPoint instances")
    ordered = tuple(
        sorted(points, key=lambda point: _instant(point.observed_at, "observed_at"))
    )
    instants = tuple(_instant(point.observed_at, "observed_at") for point in ordered)
    if len(set(instants)) != len(instants):
        raise ValueError("walk-forward observations require unique timestamps")
    return ordered


def training_points_manifest_sha256(points: Sequence[TrainingPoint]) -> str:
    """Canonical content identity for the exact governed factory input population."""

    ordered = _ordered_training_points(points)
    return _canonical_digest(
        {
            "schema_version": 1,
            "kind": "autosport-factory-training-points-v1",
            "points": [
                {
                    "observed_at": _canonical_instant(point.observed_at, "observed_at"),
                    "feature": _finite(point.feature, "feature"),
                    "target": _finite(point.target, "target"),
                    "target_available_at": _canonical_instant(
                        point.target_reveal_at, "target_available_at"
                    ),
                }
                for point in ordered
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class WalkForwardEvaluationConfig:
    """Frozen promotion-grade evaluator degrees of freedom and feature identity."""

    minimum_causal_train_size: int = 2
    feature_set_id: str | None = None
    feature_definition_sha256: str | None = None
    feature_source_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.minimum_causal_train_size) is not int or self.minimum_causal_train_size < 1:
            raise ValueError("minimum_causal_train_size must be a positive integer")
        feature_values = (
            self.feature_set_id,
            self.feature_definition_sha256,
            self.feature_source_sha256,
        )
        if any(value is not None for value in feature_values):
            if not all(value is not None for value in feature_values):
                raise ValueError("frozen feature identity must be complete")
            _text(self.feature_set_id, "feature_set_id")
            _sha256(self.feature_definition_sha256, "feature_definition_sha256")
            _sha256(self.feature_source_sha256, "feature_source_sha256")

    def canonical_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": "autosport-causal-walk-forward-v1",
            "minimum_causal_train_size": self.minimum_causal_train_size,
        }
        if self.feature_set_id is not None:
            payload.update(
                {
                    "feature_set_id": self.feature_set_id,
                    "feature_definition_sha256": _sha256(
                        self.feature_definition_sha256,
                        "feature_definition_sha256",
                    ),
                    "feature_source_sha256": _sha256(
                        self.feature_source_sha256,
                        "feature_source_sha256",
                    ),
                }
            )
        return payload

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
    def config_sha256(self) -> str:
        return _canonical_digest(self.canonical_payload())

    @classmethod
    def from_frozen_text(cls, value: object) -> "WalkForwardEvaluationConfig":
        text = _text(value, "evaluation_design")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("factory evaluation_design must be frozen canonical JSON") from exc
        basic_fields = {"kind", "minimum_causal_train_size"}
        feature_fields = {
            "feature_set_id",
            "feature_definition_sha256",
            "feature_source_sha256",
        }
        if type(payload) is not dict or set(payload) not in (
            basic_fields,
            basic_fields | feature_fields,
        ):
            raise ValueError("factory evaluation_design has unsupported fields")
        if payload.get("kind") != "autosport-causal-walk-forward-v1":
            raise ValueError("factory evaluation_design kind is unsupported")
        config = cls(
            payload.get("minimum_causal_train_size"),
            payload.get("feature_set_id"),
            payload.get("feature_definition_sha256"),
            payload.get("feature_source_sha256"),
        )
        if text != config.frozen_text:
            raise ValueError("factory evaluation_design must use canonical frozen encoding")
        return config


class BaselineModel(Protocol):
    """Typed prediction/evidence contract for transparent baseline implementations."""

    model_id: str
    training_cutoff: str

    @property
    def identity_sha256(self) -> str: ...

    def predict(self, point: TrainingPoint, *, decision_at: str) -> float: ...

    def to_payload(self) -> dict[str, object]: ...


class BaselineModelFactory(Protocol):
    """Typed construction seam used by both walk-forward and final-model training."""

    model_family: str

    def fit(
        self,
        model_id: str,
        points: Sequence[TrainingPoint],
        *,
        training_cutoff: str,
    ) -> BaselineModel: ...


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
class MeanBaselineModelFactory:
    model_family: str = "mean-baseline-v1"

    def fit(
        self,
        model_id: str,
        points: Sequence[TrainingPoint],
        *,
        training_cutoff: str,
    ) -> MeanBaselineModel:
        return MeanBaselineModel.fit(
            model_id,
            points,
            training_cutoff=training_cutoff,
        )


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_id: str
    training_cutoff: str
    evaluation_at: str
    target_available_at: str
    causal_training_count: int
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
                    "causal_training_count": fold.causal_training_count,
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

    def promotion_metrics(self) -> dict[str, float]:
        """Metrics derived only from this causal walk-forward result.

        Promotion-capable guardrails must come from governed evaluation arithmetic,
        never from caller-supplied values. New protective metrics must be added here
        (or via another hash-bound evaluator) before a frozen rule can use them.
        """
        if not self.folds:
            raise ValueError("walk-forward result has no folds")
        return {
            self.primary_metric: self.primary_value,
            "max_squared_error": max(fold.squared_error for fold in self.folds),
        }


class WalkForwardRunner:
    """Causal expanding-window evaluator; evaluation labels never enter their own training set."""

    @staticmethod
    def run(
        points: Sequence[TrainingPoint],
        *,
        minimum_train_size: int = 2,
        model_factory: BaselineModelFactory | None = None,
    ) -> WalkForwardResult:
        if type(minimum_train_size) is not int or minimum_train_size < 1:
            raise ValueError("minimum_train_size must be a positive integer")
        factory = model_factory or MeanBaselineModelFactory()
        _text(factory.model_family, "model_family")
        ordered = _ordered_training_points(points)
        if len(ordered) <= minimum_train_size:
            raise ValueError("not enough observations for walk-forward evaluation")

        folds: list[WalkForwardFold] = []
        for index in range(1, len(ordered)):
            evaluation = ordered[index]
            train = ordered[:index]
            cutoff = train[-1].observed_at
            if _instant(cutoff, "training_cutoff") >= _instant(
                evaluation.observed_at, "evaluation_at"
            ):
                raise ValueError("walk-forward cutoff must precede evaluation")
            cutoff_instant = _instant(cutoff, "training_cutoff")
            causal_train = tuple(
                point
                for point in train
                if _instant(point.observed_at, "observed_at") <= cutoff_instant
                and _instant(point.target_reveal_at, "target_available_at") <= cutoff_instant
            )
            if len(causal_train) < minimum_train_size:
                continue
            model = factory.fit(
                f"{factory.model_family}-fold-{index}",
                causal_train,
                training_cutoff=cutoff,
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
                    causal_training_count=len(causal_train),
                    prediction=prediction,
                    target=target,
                    squared_error=squared_error,
                )
            )
        if not folds:
            raise ValueError(
                "not enough causally revealed training labels for walk-forward evaluation"
            )
        mse = sum(fold.squared_error for fold in folds) / len(folds)
        return WalkForwardResult(factory.model_family, tuple(folds), "mse", mse)


class PromotionVerdict(StrEnum):
    PROMOTE = "PROMOTE"
    REJECT = "REJECT"
    INCONCLUSIVE = "INCONCLUSIVE"


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
        promotion_evidence: PromotionEvidence | None = None,
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
        if promotion_evidence is None:
            return PromotionEvaluation(
                PromotionVerdict.INCONCLUSIVE,
                PromotionAction.RETAIN,
                improvement,
                ("missing typed promotion evidence",),
            )
        evidence_reasons: list[str] = []
        if promotion_evidence.validity is not PromotionEvidenceValidity.ELIGIBLE:
            evidence_reasons.append("promotion evidence is not eligible")
        if promotion_evidence.holdout_consumed:
            evidence_reasons.append("confirmation holdout already consumed")
        if promotion_evidence.effective_sample_size < promotion_evidence.minimum_effective_sample_size:
            evidence_reasons.append("effective sample size below frozen minimum")
        if promotion_evidence.guardrails_passed is not True:
            evidence_reasons.append("promotion guardrails are not satisfied")
        if promotion_evidence.estimand != rule.primary_metric:
            evidence_reasons.append("promotion evidence estimand does not match primary metric")
        if promotion_evidence.direction is not PromotionEvidenceDirection.LOWER_IS_BETTER:
            evidence_reasons.append("promotion evidence direction does not match frozen metric direction")
        if promotion_evidence.rollback_identity != rollback_target:
            evidence_reasons.append("promotion evidence rollback identity does not match")
        try:
            practical = Decimal(promotion_evidence.practical_improvement)
            interval_low = Decimal(promotion_evidence.effect_interval_low)
        except Exception:
            evidence_reasons.append("promotion evidence numeric payload is invalid")
            practical = Decimal(0)
            interval_low = Decimal(0)
        if practical <= 0 or interval_low <= 0:
            evidence_reasons.append("promotion evidence does not establish strictly positive improvement")
        if practical < Decimal(str(rule.minimum_improvement)) or interval_low < Decimal(str(rule.minimum_improvement)):
            evidence_reasons.append("promotion evidence does not clear the frozen minimum improvement")
        if evidence_reasons:
            return PromotionEvaluation(
                PromotionVerdict.INCONCLUSIVE,
                PromotionAction.RETAIN,
                improvement,
                tuple(evidence_reasons),
            )
        return PromotionEvaluation(
            PromotionVerdict.PROMOTE,
            PromotionAction.PROMOTE,
            improvement,
            ("frozen promotion rule and typed evidence satisfied",),
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

    def exists(self, kind: str, identity: str) -> bool:
        """Return whether an immutable artifact identity is already occupied."""
        return self._path(kind, identity).exists()

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
        self,
        registry: ScientificRegistry,
        artifact_store: FactoryArtifactStore,
        *,
        baseline_model_factory: BaselineModelFactory | None = None,
    ) -> None:
        if not isinstance(registry, ScientificRegistry):
            raise ValueError("registry must be ScientificRegistry")
        self.registry = registry
        self.artifact_store = artifact_store
        self.baseline_model_factory = baseline_model_factory or MeanBaselineModelFactory()
        _text(self.baseline_model_factory.model_family, "model_family")

    def _foundation(
        self, spec: FactoryCandidateSpec, rule: PromotionRule
    ) -> tuple[dict[str, object], str, str, WalkForwardEvaluationConfig, str]:
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
        question_id = binding.get("research_question_id")
        hypothesis_id = binding.get("hypothesis_id")
        if type(question_id) is not str or type(hypothesis_id) is not str:
            raise ValueError("research protocol lacks frozen question/hypothesis identity")
        question = self.registry.get("ResearchQuestion", question_id)
        hypothesis = self.registry.get("Hypothesis", hypothesis_id)
        if question is None or hypothesis is None:
            raise ValueError("frozen question/hypothesis is missing")
        if hypothesis.payload.get("research_question_id") != question_id:
            raise ValueError("frozen hypothesis does not reference frozen research question")
        if _canonical_digest(question.payload) != _sha256(
            binding.get("research_question_sha256"), "research_question_sha256"
        ):
            raise ValueError("research question does not match frozen research protocol")
        if _canonical_digest(hypothesis.payload) != _sha256(
            binding.get("hypothesis_sha256"), "hypothesis_sha256"
        ):
            raise ValueError("hypothesis does not match frozen research protocol")

        question_available = _instant(question.available_at, "research question available_at")
        hypothesis_available = _instant(hypothesis.available_at, "hypothesis available_at")
        feature_available = _instant(feature.available_at, "feature set available_at")
        protocol_frozen = _instant(binding.get("frozen_at_utc"), "protocol frozen_at_utc")
        protocol_available = _instant(protocol.available_at, "research protocol available_at")
        dataset_available = _instant(dataset.available_at, "dataset snapshot available_at")
        experiment_start = _instant(spec.created_at, "experiment created_at")
        if question_available > hypothesis_available:
            raise ValueError("research question must precede frozen hypothesis")
        if hypothesis_available > protocol_frozen:
            raise ValueError("hypothesis must precede research protocol freeze")
        if feature_available > protocol_frozen:
            raise ValueError("feature set must be available by research protocol freeze")
        if protocol_frozen > protocol_available:
            raise ValueError("research protocol cannot be persisted before its freeze time")
        if protocol_available > dataset_available:
            raise ValueError("dataset snapshot must not precede durable research protocol")
        if dataset_available > experiment_start:
            raise ValueError("scientific foundation was not available by experiment start")

        if hypothesis.payload.get("primary_metric") != rule.primary_metric:
            raise ValueError("promotion primary metric does not match frozen hypothesis")
        protective = hypothesis.payload.get("protective_metrics")
        if type(protective) is not list:
            raise ValueError("frozen hypothesis protective metrics are invalid")
        rule_protective = {name for name, _ in rule.protective_metric_maxima}
        if not rule_protective.issubset(set(protective)):
            raise ValueError("promotion protective metrics are not frozen in hypothesis")
        dataset_manifest_sha256 = _sha256(
            dataset.payload.get("manifest_sha256"), "dataset manifest_sha256"
        )
        if dataset_manifest_sha256 != _sha256(
            protocol.payload.get("dataset_manifest_sha256"),
            "protocol dataset_manifest_sha256",
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
        evaluation_config = WalkForwardEvaluationConfig.from_frozen_text(
            binding.get("evaluation_design")
        )
        if evaluation_config.feature_set_id is None:
            raise ValueError("frozen evaluator config lacks canonical feature identity")
        if feature.payload.get("feature_set_id") != evaluation_config.feature_set_id:
            raise ValueError("feature set identity does not match frozen evaluator config")
        if _sha256(
            feature.payload.get("definition_sha256"), "feature definition_sha256"
        ) != _sha256(
            evaluation_config.feature_definition_sha256,
            "frozen feature_definition_sha256",
        ):
            raise ValueError("feature definition does not match frozen evaluator config")
        if _sha256(
            feature.payload.get("source_sha256"), "feature source_sha256"
        ) != _sha256(
            evaluation_config.feature_source_sha256,
            "frozen feature_source_sha256",
        ):
            raise ValueError("feature source does not match frozen evaluator config")
        config_sha256 = _sha256(binding.get("code_config_sha256"), "code_config_sha256")
        return (
            binding,
            config_sha256,
            protocol.payload["protocol_sha256"],
            evaluation_config,
            dataset_manifest_sha256,
        )

    def _durable_champion_metrics(
        self,
        *,
        champion_strategy_version_id: str,
        as_of: str,
        research_protocol_id: str,
        protocol_sha256: str,
        dataset_snapshot_id: str,
        evaluator_source_sha256: str,
        evaluator_config_sha256: str,
        training_points_manifest_sha256: str,
    ) -> tuple[str, dict[str, float]]:
        strategy = self.registry.get("StrategyVersion", champion_strategy_version_id)
        if strategy is None:
            raise ValueError("durable champion strategy is missing")
        champion_model_version_id = strategy.payload.get("model_version_id")
        if type(champion_model_version_id) is not str:
            raise ValueError("durable champion model identity is missing")

        decisions = tuple(
            decision
            for decision in self.registry.causal_records("PromotionDecision", as_of=as_of)
            if decision.payload.get("action") == PromotionAction.PROMOTE.value
            and decision.payload.get("candidate_strategy_version_id")
            == champion_strategy_version_id
        )
        if not decisions:
            raise ValueError("durable champion has no canonical promotion evidence")
        decision = decisions[-1]
        if decision.payload.get("research_protocol_id") != research_protocol_id:
            raise ValueError("champion comparison uses a different frozen research protocol")
        if decision.payload.get("protocol_sha256") != protocol_sha256:
            raise ValueError("champion comparison protocol hash mismatch")
        champion_evaluation_bundle_id = decision.payload.get("evaluation_bundle_id")
        if type(champion_evaluation_bundle_id) is not str:
            raise ValueError("canonical champion promotion lacks evaluation bundle identity")

        bundle = self.registry.get("EvaluationBundle", champion_evaluation_bundle_id)
        if bundle is None:
            raise ValueError("durable champion evaluation provenance is missing")
        if bundle.payload.get("bundle_sha256") != decision.payload.get(
            "evaluation_bundle_sha256"
        ):
            raise ValueError("canonical champion promotion/evaluation hash mismatch")
        if bundle.payload.get("dataset_snapshot_id") != dataset_snapshot_id:
            raise ValueError("champion comparison uses a different dataset snapshot")
        if _sha256(
            bundle.payload.get("evaluator_source_sha256"),
            "champion evaluator_source_sha256",
        ) != _sha256(evaluator_source_sha256, "candidate evaluator_source_sha256"):
            raise ValueError("champion comparison evaluator source mismatch")
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
        metrics_payload = self.artifact_store.read(
            "metrics",
            champion_evaluation_bundle_id,
            expected_sha256=metrics_artifact_sha256,
        )
        if metrics_payload.get("kind") != "autosport-factory-metrics-v1":
            raise ValueError("champion metrics artifact kind mismatch")
        if metrics_payload.get("evaluation_bundle_id") != champion_evaluation_bundle_id:
            raise ValueError("champion metrics evaluation identity mismatch")
        if metrics_payload.get("strategy_version_id") != champion_strategy_version_id:
            raise ValueError("champion metrics strategy identity mismatch")
        if metrics_payload.get("model_version_id") != champion_model_version_id:
            raise ValueError("champion metrics model identity mismatch")
        if metrics_payload.get("source") != "causal-walk-forward-v1":
            raise ValueError("champion metrics lack causal evaluator provenance")
        if _sha256(
            metrics_payload.get("evaluator_config_sha256"),
            "champion evaluator_config_sha256",
        ) != _sha256(evaluator_config_sha256, "candidate evaluator_config_sha256"):
            raise ValueError("champion comparison evaluator config mismatch")
        if _sha256(
            metrics_payload.get("training_points_manifest_sha256"),
            "champion training_points_manifest_sha256",
        ) != _sha256(
            training_points_manifest_sha256,
            "candidate training_points_manifest_sha256",
        ):
            raise ValueError("champion comparison dataset input manifest mismatch")
        walk_forward_result_sha256 = _sha256(
            metrics_payload.get("walk_forward_result_sha256"),
            "champion walk_forward_result_sha256",
        )

        evaluation_payload = self.artifact_store.read(
            "evaluation",
            champion_evaluation_bundle_id,
            expected_sha256=bundle.payload.get("bundle_sha256"),
        )
        if evaluation_payload.get("candidate_metrics_artifact_sha256") != metrics_artifact_sha256:
            raise ValueError("champion evaluation/metrics artifact identity mismatch")
        if evaluation_payload.get("candidate_metrics_source") != "causal-walk-forward-v1":
            raise ValueError("champion evaluation lacks causal metric source")
        if _sha256(
            evaluation_payload.get("evaluator_config_sha256"),
            "champion evaluation evaluator_config_sha256",
        ) != evaluator_config_sha256:
            raise ValueError("champion evaluation evaluator config mismatch")
        if _sha256(
            evaluation_payload.get("training_points_manifest_sha256"),
            "champion evaluation training_points_manifest_sha256",
        ) != training_points_manifest_sha256:
            raise ValueError("champion evaluation dataset input manifest mismatch")
        walk_forward_payload = evaluation_payload.get("walk_forward")
        if type(walk_forward_payload) is not dict:
            raise ValueError("champion evaluation lacks walk-forward evidence")
        if _canonical_digest(walk_forward_payload) != walk_forward_result_sha256:
            raise ValueError("champion walk-forward evidence hash mismatch")
        if evaluation_payload.get("walk_forward_result_sha256") != walk_forward_result_sha256:
            raise ValueError("champion evaluation walk-forward identity mismatch")
        metrics = _metric_map(metrics_payload.get("metrics"), "champion metrics")
        if _metric_map(
            evaluation_payload.get("candidate_metrics"),
            "champion evaluation metrics",
        ) != metrics:
            raise ValueError("champion evaluation/metrics values mismatch")
        return champion_evaluation_bundle_id, metrics

    def _preflight_promotion_history_order(self, spec: FactoryCandidateSpec) -> None:
        """Reject retroactive decisions before candidate artifacts or registry rows exist."""
        proposed_key = (
            _instant(spec.decided_at, "decided_at"),
            spec.promotion_decision_id,
        )
        all_history_cutoff = "9999-12-31T23:59:59.999999+00:00"
        for decision in self.registry.causal_records(
            "PromotionDecision", as_of=all_history_cutoff
        ):
            candidate_strategy_version_id = decision.payload.get(
                "candidate_strategy_version_id"
            )
            if type(candidate_strategy_version_id) is not str:
                raise ValueError(
                    "durable promotion history lacks candidate strategy identity"
                )
            strategy = self.registry.get(
                "StrategyVersion", candidate_strategy_version_id
            )
            if strategy is None:
                raise ValueError(
                    "durable promotion history references missing candidate strategy"
                )
            if strategy.payload.get("canonical_strategy_id") != spec.canonical_strategy_id:
                continue
            durable_key = (
                _instant(decision.available_at, "PromotionDecision.available_at"),
                decision.record_id,
            )
            if durable_key > proposed_key:
                raise ValueError(
                    "promotion decision cannot be backdated before durable promotion history in its strategy context"
                )

    def run_baseline_candidate(
        self,
        spec: FactoryCandidateSpec,
        points: Sequence[TrainingPoint],
        *,
        rule: PromotionRule,
        minimum_train_size: int | None = None,
    ) -> FactoryRunResult:
        (
            binding,
            config_sha256,
            protocol_sha256,
            evaluation_config,
            dataset_manifest_sha256,
        ) = self._foundation(spec, rule)
        if minimum_train_size is not None:
            if type(minimum_train_size) is not int or minimum_train_size < 1:
                raise ValueError("minimum_train_size must be a positive integer")
            if minimum_train_size != evaluation_config.minimum_causal_train_size:
                raise ValueError(
                    "runtime minimum_train_size does not match frozen evaluator config"
                )
        effective_minimum_train_size = evaluation_config.minimum_causal_train_size
        input_manifest_sha256 = training_points_manifest_sha256(points)
        if input_manifest_sha256 != dataset_manifest_sha256:
            raise ValueError(
                "training points do not match frozen DatasetSnapshot manifest"
            )
        completed = _instant(spec.completed_at, "completed_at")
        for point in _ordered_training_points(points):
            if _instant(point.observed_at, "observed_at") > completed:
                raise ValueError(
                    "evaluation observation was not available by experiment completion"
                )
            if _instant(point.target_reveal_at, "target_available_at") > completed:
                raise ValueError(
                    "evaluation target was not revealed by experiment completion"
                )

        current_champion = self.registry.champion_strategy(
            as_of=spec.decided_at,
            canonical_strategy_id=spec.canonical_strategy_id,
        )
        if current_champion != spec.predecessor_strategy_version_id:
            raise ValueError("candidate predecessor does not match durable context champion")
        if current_champion is None:
            raise ValueError("factory challenger promotion requires a durable rollback champion")
        self._preflight_promotion_history_order(spec)
        champion_evaluation_bundle_id, champion_metrics = self._durable_champion_metrics(
            champion_strategy_version_id=current_champion,
            as_of=spec.decided_at,
            research_protocol_id=spec.research_protocol_id,
            protocol_sha256=protocol_sha256,
            dataset_snapshot_id=spec.dataset_snapshot_id,
            evaluator_source_sha256=spec.evaluator_source_sha256,
            evaluator_config_sha256=evaluation_config.config_sha256,
            training_points_manifest_sha256=input_manifest_sha256,
        )

        walk_forward = WalkForwardRunner.run(
            points,
            minimum_train_size=effective_minimum_train_size,
            model_factory=self.baseline_model_factory,
        )
        if walk_forward.primary_metric != rule.primary_metric:
            raise ValueError("walk-forward primary metric does not match frozen promotion rule")
        if any(
            _instant(fold.evaluation_at, "evaluation_at") > completed
            for fold in walk_forward.folds
        ):
            raise ValueError("evaluation observation was not available by experiment completion")
        if any(
            _instant(fold.target_available_at, "target_available_at") > completed
            for fold in walk_forward.folds
        ):
            raise ValueError("evaluation target was not revealed by experiment completion")

        final_model = self.baseline_model_factory.fit(
            spec.model_version_id,
            points,
            training_cutoff=binding["causal_cutoff"],
        )
        candidate_metrics = walk_forward.promotion_metrics()
        required_metrics = {rule.primary_metric} | {
            name for name, _ in rule.protective_metric_maxima
        }
        missing_metrics = sorted(required_metrics - set(candidate_metrics))
        if missing_metrics:
            raise ValueError(
                "frozen promotion rule requires unsupported causal metrics: "
                + ", ".join(missing_metrics)
            )
        champion_missing = sorted(required_metrics - set(champion_metrics))
        if champion_missing:
            raise ValueError(
                "durable champion evidence lacks comparable metrics: "
                + ", ".join(champion_missing)
            )
        candidate_metrics = {
            name: candidate_metrics[name] for name in sorted(required_metrics)
        }
        champion_metrics = {
            name: champion_metrics[name] for name in sorted(required_metrics)
        }
        provisional_promotion = PromotionController.evaluate(
            rule,
            champion_metrics=champion_metrics,
            challenger_metrics=candidate_metrics,
            provenance_complete=True,
            rollback_target=current_champion,
        )
        outcome = ResearchOutcome.INCONCLUSIVE
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
            notes="; ".join(provisional_promotion.reasons),
        )
        existing_experiment = self.registry.get("Experiment", spec.experiment_id)
        if existing_experiment is None:
            if self.registry.find_experiment_fingerprint(experiment.fingerprint):
                raise DuplicateExperimentFingerprintError(
                    "experiment fingerprint already has durable history; inspect negative/null results before repeating"
                )
        elif existing_experiment.payload != experiment.to_payload():
            raise ValueError(
                "conflicting immutable experiment identity must fail before factory mutation"
            )

        if existing_experiment is None:
            registry_identities = [
                ("ModelVersion", spec.model_version_id),
                ("StrategyVersion", spec.strategy_version_id),
                ("EvaluationBundle", spec.evaluation_bundle_id),
                ("PromotionDecision", spec.promotion_decision_id),
            ]
            if outcome is not ResearchOutcome.POSITIVE:
                registry_identities.append(
                    ("Postmortem", f"{spec.experiment_id}:postmortem")
                )
            for record_type, record_id in registry_identities:
                if self.registry.get(record_type, record_id) is not None:
                    raise ValueError(
                        "candidate immutable identity already exists before factory mutation: "
                        f"{record_type}:{record_id}"
                    )

            artifact_identities = (
                ("model", spec.model_version_id),
                ("metrics", spec.evaluation_bundle_id),
                ("evaluation", spec.evaluation_bundle_id),
            )
            for kind, identity in artifact_identities:
                if self.artifact_store.exists(kind, identity):
                    raise ValueError(
                        "candidate artifact identity already exists before factory mutation: "
                        f"{kind}:{identity}"
                    )

        model_payload = final_model.to_payload()
        model_payload.update(
            {
                "model_version_id": spec.model_version_id,
                "research_protocol_id": spec.research_protocol_id,
                "dataset_snapshot_id": spec.dataset_snapshot_id,
                "feature_set_id": spec.feature_set_id,
                "config_sha256": config_sha256,
                "evaluator_config_sha256": evaluation_config.config_sha256,
                "training_points_manifest_sha256": input_manifest_sha256,
                "seed": spec.seed,
            }
        )
        model_artifact_sha256 = self.artifact_store.write(
            "model", spec.model_version_id, model_payload
        )
        self.registry.append(
            ModelVersion(
                spec.model_version_id,
                self.baseline_model_factory.model_family,
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
            "source": "causal-walk-forward-v1",
            "walk_forward_result_sha256": walk_forward.result_sha256,
            "evaluator_config_sha256": evaluation_config.config_sha256,
            "training_points_manifest_sha256": input_manifest_sha256,
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
            "evaluator_source_sha256": _sha256(
                spec.evaluator_source_sha256,
                "evaluator_source_sha256",
            ),
            "evaluator_config": evaluation_config.canonical_payload(),
            "evaluator_config_sha256": evaluation_config.config_sha256,
            "training_points_manifest_sha256": input_manifest_sha256,
            "seed": spec.seed,
            "config_sha256": config_sha256,
            "walk_forward": walk_forward.to_payload(),
            "walk_forward_result_sha256": walk_forward.result_sha256,
            "candidate_metrics": candidate_metrics,
            "candidate_metrics_artifact_sha256": candidate_metrics_artifact_sha256,
            "candidate_metrics_source": "causal-walk-forward-v1",
            "champion_evaluation_bundle_id": champion_evaluation_bundle_id,
            "champion_metrics": champion_metrics,
            "promotion_verdict": provisional_promotion.verdict.value,
            "promotion_reasons": list(provisional_promotion.reasons),
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

        champion_evaluation = self.artifact_store.read("evaluation", champion_evaluation_bundle_id)
        champion_folds = {
            fold["evaluation_at"]: fold
            for fold in champion_evaluation.get("walk_forward", {}).get("folds", [])
            if isinstance(fold, dict) and isinstance(fold.get("evaluation_at"), str)
        }
        binding = protocol["payload"]["binding"]
        paired_deltas: list[Decimal] = []
        for fold in walk_forward.folds:
            prior = champion_folds.get(fold.evaluation_at)
            if prior is None:
                continue
            paired_deltas.append(
                Decimal(str(prior["squared_error"])) - Decimal(str(fold.squared_error))
            )
        if not paired_deltas:
            raise ValueError("promotion evidence requires at least one paired causal holdout fold")
        practical = sum(paired_deltas, Decimal(0)) / Decimal(len(paired_deltas))
        uncertainty_method, effect_low, effect_high = _promotion_effect_interval(
            paired_deltas,
            binding["uncertainty_method"],
        )

        def _canonical_decimal_text(value: Decimal) -> str:
            text = format(value, "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return "0" if text in ("", "-0") else text

        stopping_sha = hashlib.sha256(str(binding["stopping_rule"]).encode("utf-8")).hexdigest()
        comparison_sha = hashlib.sha256(
            str(binding["multiple_comparison_control"]).encode("utf-8")
        ).hexdigest()
        guardrails_passed = all(
            candidate_metrics[name] <= maximum
            for name, maximum in rule.protective_metric_maxima
        )
        evidence_payload = {
            "schema_version": 1,
            "experiment_id": spec.experiment_id,
            "research_protocol_id": spec.research_protocol_id,
            "research_question_id": binding["research_question_id"],
            "hypothesis_id": binding["hypothesis_id"],
            "candidate_strategy_version_id": spec.strategy_version_id,
            "candidate_model_version_id": spec.model_version_id,
            "evaluation_bundle_id": spec.evaluation_bundle_id,
            "evaluation_bundle_sha256": evaluation_bundle_sha256,
            "dataset_snapshot_id": spec.dataset_snapshot_id,
            "confirmation_trial_family_id": f"{spec.research_protocol_id}:confirmation-trial-family",
            "holdout_access_id": promotion_holdout_access_id(
                research_protocol_id=spec.research_protocol_id,
                dataset_manifest_sha256=dataset.payload.get("manifest_sha256"),
                source_identity=dataset.payload.get("source_identity"),
                license_identity=dataset.payload.get("license_identity"),
                confirmation_trial_family_id=f"{spec.research_protocol_id}:confirmation-trial-family",
            ),
            "estimand": rule.primary_metric,
            "direction": PromotionEvidenceDirection.LOWER_IS_BETTER.value,
            "cohort_id": spec.dataset_snapshot_id,
            "effective_sample_size": len(paired_deltas),
            "minimum_effective_sample_size": 2,
            "effect_interval_low": _canonical_decimal_text(effect_low),
            "effect_interval_high": _canonical_decimal_text(effect_high),
            "practical_improvement": _canonical_decimal_text(practical),
            "guardrails_passed": guardrails_passed,
            "validity": (
                PromotionEvidenceValidity.ELIGIBLE.value
                if len(paired_deltas) >= 2
                and effect_low >= Decimal(str(rule.minimum_improvement))
                and practical >= Decimal(str(rule.minimum_improvement))
                and guardrails_passed
                else PromotionEvidenceValidity.INCONCLUSIVE.value
            ),
            "holdout_consumed": False,
            "stopping_rule_sha256": stopping_sha,
            "multiple_comparison_control_sha256": comparison_sha,
            "rollback_identity": current_champion,
            "uncertainty_method": uncertainty_method,
            "created_at": spec.decided_at,
        }
        evidence_id = hashlib.sha256(
            json.dumps(
                evidence_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        typed_evidence_payload = dict(evidence_payload)
        typed_evidence_payload["direction"] = PromotionEvidenceDirection(
            evidence_payload["direction"]
        )
        typed_evidence_payload["validity"] = PromotionEvidenceValidity(
            evidence_payload["validity"]
        )
        promotion_evidence = PromotionEvidence(
            promotion_evidence_id=evidence_id,
            **typed_evidence_payload,
        )
        self.registry.append(promotion_evidence)
        promotion = PromotionController.evaluate(
            rule,
            champion_metrics=champion_metrics,
            challenger_metrics=candidate_metrics,
            provenance_complete=True,
            rollback_target=current_champion,
            promotion_evidence=promotion_evidence,
        )
        outcome = (
            ResearchOutcome.POSITIVE
            if promotion.verdict is PromotionVerdict.PROMOTE
            else ResearchOutcome.INCONCLUSIVE
        )
        experiment = replace(experiment, outcome=outcome, notes="; ".join(promotion.reasons))
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
                promotion_evidence_id=promotion_evidence.promotion_evidence_id,
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
        dataset_snapshot_id = experiment.payload.get("dataset_snapshot_id")
        research_protocol_id = experiment.payload.get("research_protocol_id")
        if not all(
            isinstance(value, str)
            for value in (
                evaluation_bundle_id,
                model_version_id,
                strategy_version_id,
                dataset_snapshot_id,
                research_protocol_id,
            )
        ):
            raise ValueError("experiment lineage is incomplete after restart")
        bundle = registry.get("EvaluationBundle", evaluation_bundle_id)
        model = registry.get("ModelVersion", model_version_id)
        strategy = registry.get("StrategyVersion", strategy_version_id)
        dataset = registry.get("DatasetSnapshot", dataset_snapshot_id)
        protocol = registry.get("ResearchProtocol", research_protocol_id)
        if bundle is None or model is None or strategy is None or dataset is None or protocol is None:
            raise ValueError("factory lineage is incomplete after restart")
        binding = protocol.payload.get("binding")
        if type(binding) is not dict:
            raise ValueError("research protocol lacks frozen binding after restart")
        evaluation_config = WalkForwardEvaluationConfig.from_frozen_text(
            binding.get("evaluation_design")
        )
        dataset_manifest_sha256 = _sha256(
            dataset.payload.get("manifest_sha256"), "dataset manifest_sha256"
        )
        if dataset_manifest_sha256 != _sha256(
            protocol.payload.get("dataset_manifest_sha256"),
            "protocol dataset_manifest_sha256",
        ):
            raise ValueError("dataset manifest does not match protocol after restart")

        store = FactoryArtifactStore(artifact_root)
        evaluation_payload = store.read(
            "evaluation",
            evaluation_bundle_id,
            expected_sha256=bundle.payload["bundle_sha256"],
        )
        if evaluation_payload.get("evaluator_source_sha256") != bundle.payload.get(
            "evaluator_source_sha256"
        ):
            raise ValueError("evaluation artifact evaluator identity mismatch")
        if _sha256(
            evaluation_payload.get("evaluator_config_sha256"),
            "evaluation evaluator_config_sha256",
        ) != evaluation_config.config_sha256:
            raise ValueError("evaluation artifact evaluator config mismatch")
        if evaluation_payload.get("evaluator_config") != evaluation_config.canonical_payload():
            raise ValueError("evaluation artifact frozen evaluator payload mismatch")
        if _sha256(
            evaluation_payload.get("training_points_manifest_sha256"),
            "evaluation training_points_manifest_sha256",
        ) != dataset_manifest_sha256:
            raise ValueError("evaluation artifact dataset input manifest mismatch")

        model_payload = store.read(
            "model",
            model_version_id,
            expected_sha256=model.payload["artifact_sha256"],
        )
        if _sha256(
            model_payload.get("evaluator_config_sha256"),
            "model evaluator_config_sha256",
        ) != evaluation_config.config_sha256:
            raise ValueError("model artifact evaluator config mismatch")
        if _sha256(
            model_payload.get("training_points_manifest_sha256"),
            "model training_points_manifest_sha256",
        ) != dataset_manifest_sha256:
            raise ValueError("model artifact dataset input manifest mismatch")

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
        if metrics_payload.get("source") != "causal-walk-forward-v1":
            raise ValueError("metrics artifact lacks causal evaluator provenance")
        if _sha256(
            metrics_payload.get("evaluator_config_sha256"),
            "metrics evaluator_config_sha256",
        ) != evaluation_config.config_sha256:
            raise ValueError("metrics artifact evaluator config mismatch")
        if _sha256(
            metrics_payload.get("training_points_manifest_sha256"),
            "metrics training_points_manifest_sha256",
        ) != dataset_manifest_sha256:
            raise ValueError("metrics artifact dataset input manifest mismatch")
        walk_forward_result_sha256 = _canonical_digest(
            evaluation_payload.get("walk_forward", {})
        )
        if metrics_payload.get("walk_forward_result_sha256") != walk_forward_result_sha256:
            raise ValueError("metrics artifact walk-forward hash mismatch")
        if evaluation_payload.get("walk_forward_result_sha256") != walk_forward_result_sha256:
            raise ValueError("evaluation artifact walk-forward identity mismatch")
        if evaluation_payload.get("candidate_metrics_source") != "causal-walk-forward-v1":
            raise ValueError("evaluation artifact lacks causal metric source")
        if _metric_map(
            evaluation_payload.get("candidate_metrics"),
            "evaluation candidate metrics",
        ) != _metric_map(metrics_payload.get("metrics"), "metrics artifact metrics"):
            raise ValueError("evaluation/metrics artifact values mismatch")
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