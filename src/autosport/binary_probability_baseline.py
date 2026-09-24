from __future__ import annotations

"""Explicit Bernoulli-probability baseline for the canonical strategy-model factory.

The generic ``MeanBaselineModel`` deliberately owns only numeric prediction semantics.
This module provides a separate, closed model family whose executable identity says
exactly what a product forecast needs: training targets are Bernoulli outcomes and
the model output is the probability of outcome value 1.  It reuses the existing
StrategyModelFactory protocols and persistence; it owns no registry, promotion,
portfolio, risk, provider-write, or execution authority.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import ClassVar, Mapping, Sequence

from .strategy_model_factory import TrainingPoint, training_points_manifest_sha256


MODEL_FAMILY = "binary-outcome-mean-probability-v1"
TARGET_SEMANTICS = "bernoulli-outcome-v1"
PREDICTION_SEMANTICS = "probability-of-bernoulli-outcome-v1"
PROBABILITY_PRECISION = 34
PROBABILITY_ROUNDING = "ROUND_HALF_EVEN"


class BinaryProbabilityModelError(ValueError):
    """Raised when binary probability model evidence is ambiguous or non-causal."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BinaryProbabilityModelError(f"{name} must be a canonical non-empty string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise BinaryProbabilityModelError(f"{name} must be canonical SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BinaryProbabilityModelError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BinaryProbabilityModelError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _binary_target(value: object) -> int:
    if isinstance(value, bool) or type(value) not in (int, float):
        raise BinaryProbabilityModelError("probability training target must be numeric 0 or 1")
    numeric = float(value)
    if numeric == 0.0:
        return 0
    if numeric == 1.0:
        return 1
    raise BinaryProbabilityModelError(
        "probability training target must be an exact Bernoulli outcome 0 or 1"
    )


def _causal_training_points(
    points: Sequence[TrainingPoint],
    *,
    training_cutoff: str,
) -> tuple[TrainingPoint, ...]:
    cutoff = _instant(training_cutoff, "training_cutoff")
    eligible: list[TrainingPoint] = []
    for point in points:
        if not isinstance(point, TrainingPoint):
            raise BinaryProbabilityModelError(
                "probability training inputs must be canonical TrainingPoint values"
            )
        # Validate the complete governed population, not only the currently revealed
        # prefix, so this model family can never be fitted to a mixed target domain.
        _binary_target(point.target)
        observed = _instant(point.observed_at, "training observed_at")
        try:
            revealed_text = point.target_reveal_at
        except ValueError as exc:
            raise BinaryProbabilityModelError(
                "probability training target requires causal reveal time"
            ) from exc
        revealed = _instant(revealed_text, "training target_available_at")
        if observed <= cutoff and revealed <= cutoff:
            eligible.append(point)
    if not eligible:
        raise BinaryProbabilityModelError(
            "probability baseline requires at least one causally revealed outcome"
        )
    eligible.sort(
        key=lambda point: (
            _instant(point.observed_at, "training observed_at"),
            point.evidence_sha256s,
        )
    )
    return tuple(eligible)


def _canonical_decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise BinaryProbabilityModelError("probability must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


@dataclass(frozen=True, slots=True)
class BinaryOutcomeMeanProbabilityModel:
    """Empirical Bernoulli mean with explicit product probability semantics."""

    model_id: str
    training_cutoff: str
    success_count: int
    training_count: int
    causal_training_manifest_sha256: str

    model_family: ClassVar[str] = MODEL_FAMILY
    target_semantics: ClassVar[str] = TARGET_SEMANTICS
    prediction_semantics: ClassVar[str] = PREDICTION_SEMANTICS

    def __post_init__(self) -> None:
        _text(self.model_id, "model_id")
        _instant(self.training_cutoff, "training_cutoff")
        if type(self.training_count) is not int or self.training_count <= 0:
            raise BinaryProbabilityModelError("training_count must be a positive integer")
        if (
            type(self.success_count) is not int
            or self.success_count < 0
            or self.success_count > self.training_count
        ):
            raise BinaryProbabilityModelError(
                "success_count must be an integer between 0 and training_count"
            )
        _sha256(
            self.causal_training_manifest_sha256,
            "causal_training_manifest_sha256",
        )

    @property
    def probability(self) -> Decimal:
        context = Context(prec=PROBABILITY_PRECISION, rounding=ROUND_HALF_EVEN)
        with localcontext(context):
            value = Decimal(self.success_count) / Decimal(self.training_count)
        if value < 0 or value > 1:
            raise BinaryProbabilityModelError("reconstructed probability is outside [0,1]")
        return value

    def predict_probability(self, *, observed_at: str, decision_at: str) -> Decimal:
        observed = _instant(observed_at, "prediction observed_at")
        decision = _instant(decision_at, "prediction decision_at")
        if observed > decision:
            raise BinaryProbabilityModelError(
                "prediction input is not available at decision time"
            )
        if _instant(self.training_cutoff, "training_cutoff") > decision:
            raise BinaryProbabilityModelError(
                "model training cutoff exceeds decision time"
            )
        return self.probability

    def predict(self, point: TrainingPoint, *, decision_at: str) -> float:
        if not isinstance(point, TrainingPoint):
            raise BinaryProbabilityModelError(
                "prediction input must be a canonical TrainingPoint"
            )
        return float(
            self.predict_probability(
                observed_at=point.observed_at,
                decision_at=decision_at,
            )
        )

    def _identity_payload(self) -> dict[str, object]:
        return {
            "family": self.model_family,
            "model_id": self.model_id,
            "training_cutoff": self.training_cutoff,
            "success_count": self.success_count,
            "training_count": self.training_count,
            "causal_training_manifest_sha256": self.causal_training_manifest_sha256,
            "target_semantics": self.target_semantics,
            "prediction_semantics": self.prediction_semantics,
            "probability_arithmetic": {
                "kind": "decimal-rational-v1",
                "precision": PROBABILITY_PRECISION,
                "rounding": PROBABILITY_ROUNDING,
                "numerator": "success_count",
                "denominator": "training_count",
            },
        }

    @property
    def identity_sha256(self) -> str:
        return _canonical_digest(self._identity_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            **self._identity_payload(),
            "probability": _canonical_decimal_text(self.probability),
            "identity_sha256": self.identity_sha256,
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> "BinaryOutcomeMeanProbabilityModel":
        if payload.get("schema_version") != 1 or type(payload.get("schema_version")) is not int:
            raise BinaryProbabilityModelError("unsupported probability artifact schema")
        if payload.get("family") != MODEL_FAMILY:
            raise BinaryProbabilityModelError("probability artifact family mismatch")
        if payload.get("target_semantics") != TARGET_SEMANTICS:
            raise BinaryProbabilityModelError("probability artifact target semantics mismatch")
        if payload.get("prediction_semantics") != PREDICTION_SEMANTICS:
            raise BinaryProbabilityModelError(
                "probability artifact prediction semantics mismatch"
            )
        if payload.get("probability_arithmetic") != {
            "kind": "decimal-rational-v1",
            "precision": PROBABILITY_PRECISION,
            "rounding": PROBABILITY_ROUNDING,
            "numerator": "success_count",
            "denominator": "training_count",
        }:
            raise BinaryProbabilityModelError(
                "probability artifact arithmetic contract mismatch"
            )
        model = cls(
            model_id=_text(payload.get("model_id"), "model_id"),
            training_cutoff=_text(payload.get("training_cutoff"), "training_cutoff"),
            success_count=payload.get("success_count"),  # type: ignore[arg-type]
            training_count=payload.get("training_count"),  # type: ignore[arg-type]
            causal_training_manifest_sha256=_sha256(
                payload.get("causal_training_manifest_sha256"),
                "causal_training_manifest_sha256",
            ),
        )
        if payload.get("probability") != _canonical_decimal_text(model.probability):
            raise BinaryProbabilityModelError("probability artifact value mismatch")
        if _sha256(payload.get("identity_sha256"), "identity_sha256") != model.identity_sha256:
            raise BinaryProbabilityModelError("probability artifact identity mismatch")
        return model


@dataclass(frozen=True, slots=True)
class BinaryOutcomeMeanProbabilityFactory:
    """Factory adapter for the existing causal walk-forward/product factory protocol."""

    model_family: ClassVar[str] = MODEL_FAMILY

    def fit(
        self,
        model_id: str,
        points: Sequence[TrainingPoint],
        *,
        training_cutoff: str,
    ) -> BinaryOutcomeMeanProbabilityModel:
        _text(model_id, "model_id")
        eligible = _causal_training_points(
            points,
            training_cutoff=training_cutoff,
        )
        successes = sum(_binary_target(point.target) for point in eligible)
        return BinaryOutcomeMeanProbabilityModel(
            model_id=model_id,
            training_cutoff=training_cutoff,
            success_count=successes,
            training_count=len(eligible),
            causal_training_manifest_sha256=training_points_manifest_sha256(eligible),
        )


__all__ = [
    "BinaryOutcomeMeanProbabilityFactory",
    "BinaryOutcomeMeanProbabilityModel",
    "BinaryProbabilityModelError",
    "MODEL_FAMILY",
    "PREDICTION_SEMANTICS",
    "PROBABILITY_PRECISION",
    "PROBABILITY_ROUNDING",
    "TARGET_SEMANTICS",
]
