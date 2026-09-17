from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping, Sequence

from .scientific_registry import PromotionAction


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


@dataclass(frozen=True, slots=True)
class TrainingPoint:
    observed_at: str
    feature: float
    target: float

    def __post_init__(self) -> None:
        if type(self.observed_at) is not str or not self.observed_at:
            raise ValueError("observed_at must be a non-empty timestamp identity")
        _finite(self.feature, "feature")
        _finite(self.target, "target")


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
        if type(model_id) is not str or not model_id:
            raise ValueError("model_id must be non-empty")
        if type(training_cutoff) is not str or not training_cutoff:
            raise ValueError("training_cutoff must be non-empty")
        eligible = tuple(point for point in points if point.observed_at <= training_cutoff)
        if not eligible:
            raise ValueError("baseline requires causal training observations")
        if any(point.observed_at > training_cutoff for point in eligible):
            raise ValueError("future training observation")
        mean = sum(_finite(point.target, "target") for point in eligible) / len(eligible)
        return cls(model_id, training_cutoff, mean, len(eligible))

    def predict(self, point: TrainingPoint, *, decision_at: str) -> float:
        if point.observed_at > decision_at:
            raise ValueError("prediction input is not available at decision time")
        if self.training_cutoff > decision_at:
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


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    fold_id: str
    training_cutoff: str
    evaluation_at: str
    prediction: float
    target: float
    squared_error: float


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    model_family: str
    folds: tuple[WalkForwardFold, ...]
    primary_metric: str
    primary_value: float

    @property
    def result_sha256(self) -> str:
        return _canonical_digest(
            {
                "model_family": self.model_family,
                "primary_metric": self.primary_metric,
                "primary_value": self.primary_value,
                "folds": [
                    {
                        "fold_id": fold.fold_id,
                        "training_cutoff": fold.training_cutoff,
                        "evaluation_at": fold.evaluation_at,
                        "prediction": fold.prediction,
                        "target": fold.target,
                        "squared_error": fold.squared_error,
                    }
                    for fold in self.folds
                ],
            }
        )


class WalkForwardRunner:
    """Causal expanding-window evaluator; evaluation rows never enter their own training set."""

    @staticmethod
    def run(points: Sequence[TrainingPoint], *, minimum_train_size: int = 2) -> WalkForwardResult:
        if type(minimum_train_size) is not int or minimum_train_size < 1:
            raise ValueError("minimum_train_size must be a positive integer")
        ordered = tuple(sorted(points, key=lambda point: point.observed_at))
        if len(ordered) <= minimum_train_size:
            raise ValueError("not enough observations for walk-forward evaluation")
        if len({point.observed_at for point in ordered}) != len(ordered):
            raise ValueError("walk-forward observations require unique timestamps")

        folds: list[WalkForwardFold] = []
        for index in range(minimum_train_size, len(ordered)):
            evaluation = ordered[index]
            train = ordered[:index]
            cutoff = train[-1].observed_at
            if cutoff >= evaluation.observed_at:
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
        if type(self.primary_metric) is not str or not self.primary_metric:
            raise ValueError("primary_metric must be non-empty")
        improvement = _finite(self.minimum_improvement, "minimum_improvement")
        if improvement < 0:
            raise ValueError("minimum_improvement must be non-negative")
        names: set[str] = set()
        for name, maximum in self.protective_metric_maxima:
            if type(name) is not str or not name or name in names:
                raise ValueError("protective metric names must be unique and non-empty")
            names.add(name)
            _finite(maximum, f"protective maximum {name}")


@dataclass(frozen=True, slots=True)
class PromotionEvaluation:
    verdict: PromotionVerdict
    registry_action: PromotionAction
    primary_improvement: float
    reasons: tuple[str, ...]


class PromotionController:
    """Deterministic fail-closed comparison; it records a verdict but owns no execution authority."""

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
        challenger = _finite(challenger_metrics[rule.primary_metric], "challenger primary metric")
        improvement = champion - challenger  # lower-is-better metrics such as MSE/log-loss
        reasons: list[str] = []
        if improvement < rule.minimum_improvement:
            reasons.append("primary improvement below frozen threshold")
        for name, maximum in rule.protective_metric_maxima:
            if name not in challenger_metrics:
                reasons.append(f"missing protective metric: {name}")
                continue
            if _finite(challenger_metrics[name], f"challenger protective metric {name}") > maximum:
                reasons.append(f"protective metric degraded: {name}")
        if reasons:
            return PromotionEvaluation(
                PromotionVerdict.REJECT, PromotionAction.REJECT, improvement, tuple(reasons)
            )
        return PromotionEvaluation(
            PromotionVerdict.PROMOTE,
            PromotionAction.PROMOTE,
            improvement,
            ("frozen promotion rule satisfied",),
        )


@dataclass(frozen=True, slots=True)
class DriftEvidence:
    metric_name: str
    reference_value: float
    current_value: float
    absolute_threshold: float
    observed_at: str

    @property
    def drifted(self) -> bool:
        reference = _finite(self.reference_value, "reference_value")
        current = _finite(self.current_value, "current_value")
        threshold = _finite(self.absolute_threshold, "absolute_threshold")
        if threshold < 0:
            raise ValueError("absolute_threshold must be non-negative")
        return abs(current - reference) > threshold


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
