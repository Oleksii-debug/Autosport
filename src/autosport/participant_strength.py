"""Causal participant-strength forecast models built on canonical rating snapshots.

This module adapts the existing opponent-intelligence and Strategy/Model Factory
authorities.  It owns no identity, promotion, portfolio/risk, execution, or
provider-write authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, ClassVar, Mapping, Sequence

from .forecasting import ForecastRecord
from .opponent_intelligence import RatingSnapshot, SnapshotState
from .participant_identity import IdentityView
from .scientific_registry import ScientificRegistry
from .strategy_model_factory import (
    FactoryArtifactStore,
    TrainingPoint,
    training_points_manifest_sha256,
)


class ParticipantStrengthError(ValueError):
    """Raised when participant-strength evidence is non-causal or inconsistent."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ParticipantStrengthError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ParticipantStrengthError(f"{name} must be canonical SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParticipantStrengthError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ParticipantStrengthError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ParticipantStrengthError(f"{name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ParticipantStrengthError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ParticipantStrengthError(f"{name} must be a finite decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ParticipantStrengthError("decimal must be finite")
    text = format(value.normalize(), "f")
    return "0" if text in ("", "-0") else text


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _baseline_probability(feature: object) -> Decimal:
    value = _decimal(feature, "rating difference")
    if value < -1 or value > 1:
        raise ParticipantStrengthError("rating difference must be between -1 and 1")
    probability = (Decimal(1) + value) / Decimal(2)
    return min(Decimal(1), max(Decimal(0), probability))


def _causal_training_points(
    points: Sequence[TrainingPoint], *, training_cutoff: str
) -> tuple[TrainingPoint, ...]:
    cutoff = _instant(training_cutoff, "training_cutoff")
    eligible: list[TrainingPoint] = []
    for point in points:
        if not isinstance(point, TrainingPoint):
            raise ParticipantStrengthError("training points must be TrainingPoint instances")
        feature = _decimal(point.feature, "training feature")
        target = _decimal(point.target, "training target")
        if feature < -1 or feature > 1:
            raise ParticipantStrengthError(
                "participant-strength training feature must be between -1 and 1"
            )
        if target not in (Decimal(0), Decimal(1)):
            raise ParticipantStrengthError(
                "participant-strength training target must be binary"
            )
        observed = _instant(point.observed_at, "observed_at")
        revealed = _instant(point.target_reveal_at, "target_available_at")
        if observed <= cutoff and revealed <= cutoff:
            eligible.append(point)
    if not eligible:
        raise ParticipantStrengthError(
            "participant-strength model requires causally revealed training evidence"
        )
    eligible.sort(
        key=lambda item: (
            _instant(item.observed_at, "observed_at"),
            item.evidence_sha256s,
        )
    )
    return tuple(eligible)


@dataclass(frozen=True, slots=True)
class StrengthSnapshotPair:
    """Exact decision-time rating evidence for one ordered participant matchup."""

    subject: RatingSnapshot
    opponent: RatingSnapshot
    decision_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject, RatingSnapshot) or not isinstance(
            self.opponent, RatingSnapshot
        ):
            raise ParticipantStrengthError(
                "subject and opponent must be canonical RatingSnapshot values"
            )
        if self.subject.snapshot_id == self.opponent.snapshot_id:
            raise ParticipantStrengthError("subject and opponent snapshots must be distinct")
        for snapshot in (self.subject, self.opponent):
            _sha256(snapshot.snapshot_id, "rating snapshot_id")
            if snapshot.state is not SnapshotState.SUPPORTED:
                raise ParticipantStrengthError(
                    "participant-strength forecast requires supported rating snapshots"
                )
            if snapshot.rating is None or snapshot.uncertainty is None:
                raise ParticipantStrengthError(
                    "supported rating snapshot lacks rating/uncertainty evidence"
                )
            rating = _decimal(snapshot.rating, "rating")
            uncertainty = _decimal(snapshot.uncertainty, "uncertainty")
            if rating < 0 or rating > 1:
                raise ParticipantStrengthError("rating must be between 0 and 1")
            if uncertainty < 0 or uncertainty > 1:
                raise ParticipantStrengthError("uncertainty must be between 0 and 1")
        if self.subject.participant_entity_id == self.opponent.participant_entity_id:
            raise ParticipantStrengthError("matchup requires distinct participant identities")
        same_context = (
            self.subject.sport_id == self.opponent.sport_id
            and self.subject.league_id == self.opponent.league_id
            and self.subject.market_context_id == self.opponent.market_context_id
            and self.subject.view is self.opponent.view
        )
        if not same_context:
            raise ParticipantStrengthError(
                "rating snapshots must share sport/league/market/view context"
            )
        if self.subject.view is not IdentityView.AS_KNOWN_AT_DECISION:
            raise ParticipantStrengthError(
                "forecast evidence must use AS_KNOWN_AT_DECISION identity view"
            )
        decision = _instant(self.decision_at, "decision_at")
        for snapshot in (self.subject, self.opponent):
            if _instant(snapshot.causal_cutoff, "snapshot causal_cutoff") > decision:
                raise ParticipantStrengthError(
                    "rating snapshot causal cutoff exceeds forecast decision time"
                )
            if _instant(snapshot.published_at, "snapshot published_at") > decision:
                raise ParticipantStrengthError(
                    "rating snapshot was not published by forecast decision time"
                )

    @property
    def feature(self) -> Decimal:
        return _decimal(self.subject.rating, "subject rating") - _decimal(
            self.opponent.rating, "opponent rating"
        )

    @property
    def evidence_sha256s(self) -> tuple[str, str]:
        return (
            _sha256(self.subject.snapshot_id, "subject snapshot_id"),
            _sha256(self.opponent.snapshot_id, "opponent snapshot_id"),
        )

    @property
    def descriptive_uncertainty(self) -> Decimal:
        """Bounded descriptive input radius, explicitly not a probability CI."""
        return min(
            Decimal(1),
            max(
                _decimal(self.subject.uncertainty, "subject uncertainty"),
                _decimal(self.opponent.uncertainty, "opponent uncertainty"),
            ),
        )

    def training_point(
        self, *, subject_won: bool, target_available_at: str
    ) -> TrainingPoint:
        if type(subject_won) is not bool:
            raise ParticipantStrengthError("subject_won must be boolean")
        reveal = _instant(target_available_at, "target_available_at")
        decision = _instant(self.decision_at, "decision_at")
        if reveal <= decision:
            raise ParticipantStrengthError(
                "training target must be revealed after the prediction decision"
            )
        return TrainingPoint(
            observed_at=self.decision_at,
            feature=float(self.feature),
            target=1.0 if subject_won else 0.0,
            target_available_at=target_available_at,
            evidence_sha256s=self.evidence_sha256s,
        )


@dataclass(frozen=True, slots=True)
class RatingDifferenceBaselineModel:
    """Transparent P(A wins)=0.5+(rating_A-rating_B)/2 baseline."""

    model_id: str
    training_cutoff: str
    training_count: int
    training_manifest_sha256: str

    model_family = "participant-rating-difference-baseline-v1"

    def __post_init__(self) -> None:
        _text(self.model_id, "model_id")
        _instant(self.training_cutoff, "training_cutoff")
        if type(self.training_count) is not int or self.training_count < 1:
            raise ParticipantStrengthError("training_count must be a positive integer")
        _sha256(self.training_manifest_sha256, "training_manifest_sha256")

    @property
    def identity_sha256(self) -> str:
        return _digest(
            {
                "family": self.model_family,
                "model_id": self.model_id,
                "training_cutoff": self.training_cutoff,
                "training_count": self.training_count,
                "training_manifest_sha256": self.training_manifest_sha256,
                "formula": "p=(1+rating_difference)/2",
            }
        )

    def predict_feature(self, feature: object, *, decision_at: str) -> float:
        if _instant(self.training_cutoff, "training_cutoff") > _instant(
            decision_at, "decision_at"
        ):
            raise ParticipantStrengthError("model training cutoff exceeds decision time")
        return float(_baseline_probability(feature))

    def predict(self, point: TrainingPoint, *, decision_at: str) -> float:
        if _instant(point.observed_at, "observed_at") > _instant(
            decision_at, "decision_at"
        ):
            raise ParticipantStrengthError(
                "prediction input is not available at decision time"
            )
        return self.predict_feature(point.feature, decision_at=decision_at)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "family": self.model_family,
            "model_id": self.model_id,
            "training_cutoff": self.training_cutoff,
            "training_count": self.training_count,
            "training_manifest_sha256": self.training_manifest_sha256,
            "formula": "p=(1+rating_difference)/2",
            "identity_sha256": self.identity_sha256,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> "RatingDifferenceBaselineModel":
        if payload.get("family") != cls.model_family:
            raise ParticipantStrengthError("baseline artifact family mismatch")
        model = cls(
            _text(payload.get("model_id"), "model_id"),
            _text(payload.get("training_cutoff"), "training_cutoff"),
            payload.get("training_count"),  # type: ignore[arg-type]
            _sha256(
                payload.get("training_manifest_sha256"),
                "training_manifest_sha256",
            ),
        )
        identity = payload.get("identity_sha256")
        if (
            identity is not None
            and _sha256(identity, "identity_sha256") != model.identity_sha256
        ):
            raise ParticipantStrengthError("baseline artifact identity mismatch")
        return model


@dataclass(frozen=True, slots=True)
class RatingDifferenceBaselineFactory:
    model_family: ClassVar[str] = RatingDifferenceBaselineModel.model_family

    def fit(
        self,
        model_id: str,
        points: Sequence[TrainingPoint],
        *,
        training_cutoff: str,
    ) -> RatingDifferenceBaselineModel:
        eligible = _causal_training_points(points, training_cutoff=training_cutoff)
        return RatingDifferenceBaselineModel(
            model_id=model_id,
            training_cutoff=training_cutoff,
            training_count=len(eligible),
            training_manifest_sha256=training_points_manifest_sha256(eligible),
        )


@dataclass(frozen=True, slots=True)
class HistogramCalibratedStrengthModel:
    """Shrinkage histogram calibration over the transparent rating baseline."""

    model_id: str
    training_cutoff: str
    training_count: int
    training_manifest_sha256: str
    bin_count: int
    prior_weight: str
    bin_counts: tuple[int, ...]
    bin_probabilities: tuple[str | None, ...]

    model_family = "participant-rating-histogram-calibrated-v1"

    def __post_init__(self) -> None:
        _text(self.model_id, "model_id")
        _instant(self.training_cutoff, "training_cutoff")
        if type(self.training_count) is not int or self.training_count < 1:
            raise ParticipantStrengthError("training_count must be a positive integer")
        _sha256(self.training_manifest_sha256, "training_manifest_sha256")
        if type(self.bin_count) is not int or self.bin_count < 2 or self.bin_count > 20:
            raise ParticipantStrengthError("bin_count must be between 2 and 20")
        prior = _decimal(self.prior_weight, "prior_weight")
        if prior <= 0:
            raise ParticipantStrengthError("prior_weight must be positive")
        if (
            len(self.bin_counts) != self.bin_count
            or len(self.bin_probabilities) != self.bin_count
        ):
            raise ParticipantStrengthError(
                "calibration bin vectors must match bin_count"
            )
        if sum(self.bin_counts) != self.training_count:
            raise ParticipantStrengthError(
                "calibration bin counts must match training_count"
            )
        for count, probability in zip(
            self.bin_counts, self.bin_probabilities, strict=True
        ):
            if type(count) is not int or count < 0:
                raise ParticipantStrengthError(
                    "calibration bin count must be non-negative"
                )
            if count == 0:
                if probability is not None:
                    raise ParticipantStrengthError(
                        "empty calibration bin must not invent a probability"
                    )
                continue
            if probability is None:
                raise ParticipantStrengthError(
                    "populated calibration bin requires a probability"
                )
            value = _decimal(probability, "calibrated probability")
            if value < 0 or value > 1:
                raise ParticipantStrengthError(
                    "calibrated probability must be between 0 and 1"
                )

    @property
    def identity_sha256(self) -> str:
        return _digest(self.to_payload(include_identity=False))

    def _bin_index(self, probability: Decimal) -> int:
        index = int(probability * Decimal(self.bin_count))
        return min(self.bin_count - 1, max(0, index))

    def predict_feature(self, feature: object, *, decision_at: str) -> float:
        if _instant(self.training_cutoff, "training_cutoff") > _instant(
            decision_at, "decision_at"
        ):
            raise ParticipantStrengthError("model training cutoff exceeds decision time")
        raw = _baseline_probability(feature)
        index = self._bin_index(raw)
        calibrated = self.bin_probabilities[index]
        return float(
            raw
            if calibrated is None
            else _decimal(calibrated, "calibrated probability")
        )

    def predict(self, point: TrainingPoint, *, decision_at: str) -> float:
        if _instant(point.observed_at, "observed_at") > _instant(
            decision_at, "decision_at"
        ):
            raise ParticipantStrengthError(
                "prediction input is not available at decision time"
            )
        return self.predict_feature(point.feature, decision_at=decision_at)

    def calibration_evidence(self) -> dict[str, object]:
        return {
            "kind": "shrinkage-histogram-calibration-v1",
            "bin_count": self.bin_count,
            "prior_weight": self.prior_weight,
            "bin_counts": list(self.bin_counts),
            "bin_probabilities": list(self.bin_probabilities),
            "training_count": self.training_count,
            "training_manifest_sha256": self.training_manifest_sha256,
        }

    def to_payload(self, *, include_identity: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "family": self.model_family,
            "model_id": self.model_id,
            "training_cutoff": self.training_cutoff,
            "training_count": self.training_count,
            "training_manifest_sha256": self.training_manifest_sha256,
            "bin_count": self.bin_count,
            "prior_weight": self.prior_weight,
            "bin_counts": list(self.bin_counts),
            "bin_probabilities": list(self.bin_probabilities),
            "baseline_formula": "p=(1+rating_difference)/2",
            "calibration": "empirical-bin-rate-shrunk-to-bin-mean-baseline",
        }
        if include_identity:
            payload["identity_sha256"] = self.identity_sha256
        return payload

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> "HistogramCalibratedStrengthModel":
        if payload.get("family") != cls.model_family:
            raise ParticipantStrengthError("calibrated artifact family mismatch")
        counts = payload.get("bin_counts")
        probabilities = payload.get("bin_probabilities")
        if not isinstance(counts, list) or not isinstance(probabilities, list):
            raise ParticipantStrengthError(
                "calibration artifact bin vectors are invalid"
            )
        model = cls(
            _text(payload.get("model_id"), "model_id"),
            _text(payload.get("training_cutoff"), "training_cutoff"),
            payload.get("training_count"),  # type: ignore[arg-type]
            _sha256(
                payload.get("training_manifest_sha256"),
                "training_manifest_sha256",
            ),
            payload.get("bin_count"),  # type: ignore[arg-type]
            _text(payload.get("prior_weight"), "prior_weight"),
            tuple(counts),
            tuple(probabilities),
        )
        identity = payload.get("identity_sha256")
        if (
            identity is not None
            and _sha256(identity, "identity_sha256") != model.identity_sha256
        ):
            raise ParticipantStrengthError("calibrated artifact identity mismatch")
        return model


@dataclass(frozen=True, slots=True)
class HistogramCalibratedStrengthFactory:
    bin_count: int = 5
    prior_weight: str = "2"
    model_family: ClassVar[str] = HistogramCalibratedStrengthModel.model_family

    def __post_init__(self) -> None:
        if type(self.bin_count) is not int or self.bin_count < 2 or self.bin_count > 20:
            raise ParticipantStrengthError("bin_count must be between 2 and 20")
        if _decimal(self.prior_weight, "prior_weight") <= 0:
            raise ParticipantStrengthError("prior_weight must be positive")

    def fit(
        self,
        model_id: str,
        points: Sequence[TrainingPoint],
        *,
        training_cutoff: str,
    ) -> HistogramCalibratedStrengthModel:
        eligible = _causal_training_points(points, training_cutoff=training_cutoff)
        counts = [0] * self.bin_count
        successes = [Decimal(0)] * self.bin_count
        raw_sums = [Decimal(0)] * self.bin_count
        for point in eligible:
            raw = _baseline_probability(point.feature)
            index = min(
                self.bin_count - 1, int(raw * Decimal(self.bin_count))
            )
            counts[index] += 1
            successes[index] += _decimal(point.target, "training target")
            raw_sums[index] += raw

        prior_weight = _decimal(self.prior_weight, "prior_weight")
        probabilities: list[str | None] = []
        with localcontext() as context:
            context.prec = 28
            for index, count in enumerate(counts):
                if count == 0:
                    probabilities.append(None)
                    continue
                prior_probability = raw_sums[index] / Decimal(count)
                calibrated = (
                    successes[index] + prior_weight * prior_probability
                ) / (Decimal(count) + prior_weight)
                probabilities.append(_decimal_text(calibrated))

        return HistogramCalibratedStrengthModel(
            model_id=model_id,
            training_cutoff=training_cutoff,
            training_count=len(eligible),
            training_manifest_sha256=training_points_manifest_sha256(eligible),
            bin_count=self.bin_count,
            prior_weight=_decimal_text(prior_weight),
            bin_counts=tuple(counts),
            bin_probabilities=tuple(probabilities),
        )


StrengthModel = RatingDifferenceBaselineModel | HistogramCalibratedStrengthModel


def load_strength_model(payload: Mapping[str, object]) -> StrengthModel:
    family = payload.get("family")
    if family == RatingDifferenceBaselineModel.model_family:
        return RatingDifferenceBaselineModel.from_payload(payload)
    if family == HistogramCalibratedStrengthModel.model_family:
        return HistogramCalibratedStrengthModel.from_payload(payload)
    raise ParticipantStrengthError("unsupported participant-strength model family")


def emit_registered_strength_forecast(
    *,
    registry: ScientificRegistry,
    artifact_store: FactoryArtifactStore,
    evidence: StrengthSnapshotPair,
    model_version_id: str,
    strategy_version_id: str,
    quote_key: str,
) -> ForecastRecord:
    """Emit a forecast only from causally available registered factory evidence."""

    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be ScientificRegistry")
    if not isinstance(artifact_store, FactoryArtifactStore):
        raise TypeError("artifact_store must be FactoryArtifactStore")
    if not isinstance(evidence, StrengthSnapshotPair):
        raise TypeError("evidence must be StrengthSnapshotPair")
    model_id = _text(model_version_id, "model_version_id")
    strategy_id = _text(strategy_version_id, "strategy_version_id")
    quote = _text(quote_key, "quote_key")
    decision = _instant(evidence.decision_at, "decision_at")

    model_entry = registry.get("ModelVersion", model_id)
    strategy_entry = registry.get("StrategyVersion", strategy_id)
    if model_entry is None or strategy_entry is None:
        raise ParticipantStrengthError(
            "forecast requires registered ModelVersion and StrategyVersion"
        )
    model_available = _instant(
        model_entry.available_at, "ModelVersion.available_at"
    )
    strategy_available = _instant(
        strategy_entry.available_at, "StrategyVersion.available_at"
    )
    if model_available > decision:
        raise ParticipantStrengthError(
            "model version was not available at forecast decision time"
        )
    if strategy_available > decision:
        raise ParticipantStrengthError(
            "strategy version was not available at forecast decision time"
        )
    if model_available > strategy_available:
        raise ParticipantStrengthError(
            "model version was not available when strategy version was created"
        )
    if strategy_entry.payload.get("model_version_id") != model_id:
        raise ParticipantStrengthError(
            "strategy version does not bind the requested model version"
        )
    artifact_sha256 = _sha256(
        model_entry.payload.get("artifact_sha256"), "model artifact_sha256"
    )
    artifact = artifact_store.read(
        "model", model_id, expected_sha256=artifact_sha256
    )
    if artifact.get("model_version_id") != model_id:
        raise ParticipantStrengthError("model artifact version identity mismatch")
    if artifact.get("family") != model_entry.payload.get("model_family"):
        raise ParticipantStrengthError("model artifact family/registry mismatch")
    if artifact.get("config_sha256") != model_entry.payload.get("config_sha256"):
        raise ParticipantStrengthError("model artifact config/registry mismatch")
    if strategy_entry.payload.get("config_sha256") != model_entry.payload.get(
        "config_sha256"
    ):
        raise ParticipantStrengthError("strategy/model config identity mismatch")

    dataset_id = _text(
        model_entry.payload.get("dataset_snapshot_id"), "dataset_snapshot_id"
    )
    feature_id = _text(model_entry.payload.get("feature_set_id"), "feature_set_id")
    protocol_id = _text(
        model_entry.payload.get("research_protocol_id"), "research_protocol_id"
    )
    for field, expected in (
        ("dataset_snapshot_id", dataset_id),
        ("feature_set_id", feature_id),
        ("research_protocol_id", protocol_id),
    ):
        if artifact.get(field) != expected:
            raise ParticipantStrengthError(
                f"model artifact {field}/registry mismatch"
            )
    dataset_entry = registry.get("DatasetSnapshot", dataset_id)
    feature_entry = registry.get("FeatureSet", feature_id)
    protocol_entry = registry.get("ResearchProtocol", protocol_id)
    if dataset_entry is None or feature_entry is None or protocol_entry is None:
        raise ParticipantStrengthError(
            "registered model lacks DatasetSnapshot/FeatureSet/ResearchProtocol foundation"
        )
    binding = protocol_entry.payload.get("binding")
    if type(binding) is not dict:
        raise ParticipantStrengthError(
            "registered ResearchProtocol lacks frozen scientific binding"
        )
    question_id = _text(
        binding.get("research_question_id"), "research_question_id"
    )
    hypothesis_id = _text(binding.get("hypothesis_id"), "hypothesis_id")
    question_entry = registry.get("ResearchQuestion", question_id)
    hypothesis_entry = registry.get("Hypothesis", hypothesis_id)
    if question_entry is None or hypothesis_entry is None:
        raise ParticipantStrengthError(
            "registered ResearchProtocol lacks frozen ResearchQuestion/Hypothesis"
        )
    if hypothesis_entry.payload.get("research_question_id") != question_id:
        raise ParticipantStrengthError(
            "frozen Hypothesis does not reference frozen ResearchQuestion"
        )
    if _digest(question_entry.payload) != _sha256(
        binding.get("research_question_sha256"), "research_question_sha256"
    ):
        raise ParticipantStrengthError(
            "frozen ResearchQuestion does not match ResearchProtocol"
        )
    if _digest(hypothesis_entry.payload) != _sha256(
        binding.get("hypothesis_sha256"), "hypothesis_sha256"
    ):
        raise ParticipantStrengthError(
            "frozen Hypothesis does not match ResearchProtocol"
        )

    question_available = _instant(
        question_entry.available_at, "ResearchQuestion.available_at"
    )
    hypothesis_available = _instant(
        hypothesis_entry.available_at, "Hypothesis.available_at"
    )
    protocol_frozen = _instant(
        binding.get("frozen_at_utc"), "ResearchProtocol.frozen_at_utc"
    )
    protocol_available = _instant(
        protocol_entry.available_at, "ResearchProtocol.available_at"
    )
    feature_available = _instant(
        feature_entry.available_at, "FeatureSet.available_at"
    )
    dataset_available = _instant(
        dataset_entry.available_at, "DatasetSnapshot.available_at"
    )
    if question_available > hypothesis_available:
        raise ParticipantStrengthError(
            "ResearchQuestion must precede frozen Hypothesis"
        )
    if hypothesis_available > protocol_frozen:
        raise ParticipantStrengthError(
            "Hypothesis must precede ResearchProtocol freeze"
        )
    if feature_available > protocol_frozen:
        raise ParticipantStrengthError(
            "FeatureSet must be available by ResearchProtocol freeze"
        )
    if protocol_frozen > protocol_available:
        raise ParticipantStrengthError(
            "ResearchProtocol cannot be durable before its freeze time"
        )
    if protocol_available > dataset_available:
        raise ParticipantStrengthError(
            "DatasetSnapshot must not precede durable ResearchProtocol"
        )
    if binding.get("feature_set_version") != feature_entry.payload.get("version"):
        raise ParticipantStrengthError(
            "FeatureSet version does not match ResearchProtocol"
        )

    dataset_cutoff = _instant(
        dataset_entry.payload.get("causal_cutoff"),
        "DatasetSnapshot.causal_cutoff",
    )
    if _instant(
        binding.get("causal_cutoff"), "ResearchProtocol.causal_cutoff"
    ) != dataset_cutoff:
        raise ParticipantStrengthError(
            "ResearchProtocol/DatasetSnapshot causal cutoff mismatch"
        )
    if dataset_cutoff > model_available:
        raise ParticipantStrengthError(
            "DatasetSnapshot causal cutoff exceeds model version availability"
        )
    for record_name, entry in (
        ("DatasetSnapshot", dataset_entry),
        ("FeatureSet", feature_entry),
        ("ResearchProtocol", protocol_entry),
    ):
        if _instant(entry.available_at, f"{record_name}.available_at") > model_available:
            raise ParticipantStrengthError(
                f"{record_name} was not available when model version was created"
            )
    dataset_manifest = _sha256(
        dataset_entry.payload.get("manifest_sha256"), "dataset manifest_sha256"
    )
    if artifact.get("training_points_manifest_sha256") != dataset_manifest:
        raise ParticipantStrengthError(
            "model artifact training population does not match DatasetSnapshot manifest"
        )
    if protocol_entry.payload.get("dataset_manifest_sha256") != dataset_manifest:
        raise ParticipantStrengthError(
            "ResearchProtocol/DatasetSnapshot manifest mismatch"
        )

    model = load_strength_model(artifact)
    if model.model_id != model_id:
        raise ParticipantStrengthError("model artifact internal identity mismatch")
    if dataset_cutoff != _instant(model.training_cutoff, "model training_cutoff"):
        raise ParticipantStrengthError(
            "model training cutoff does not match DatasetSnapshot causal cutoff"
        )
    probability = Decimal(
        str(
            model.predict_feature(
                evidence.feature, decision_at=evidence.decision_at
            )
        )
    )
    uncertainty = evidence.descriptive_uncertainty
    provenance = {
        "kind": "participant-strength-forecast-v1",
        "subject_participant_entity_id": evidence.subject.participant_entity_id,
        "opponent_participant_entity_id": evidence.opponent.participant_entity_id,
        "sport_id": evidence.subject.sport_id,
        "league_id": evidence.subject.league_id,
        "market_context_id": evidence.subject.market_context_id,
        "subject_rating_snapshot_sha256": evidence.subject.snapshot_id,
        "opponent_rating_snapshot_sha256": evidence.opponent.snapshot_id,
        "subject_rating_input_digest": evidence.subject.input_digest,
        "opponent_rating_input_digest": evidence.opponent.input_digest,
        "model_artifact_sha256": artifact_sha256,
        "model_identity_sha256": model.identity_sha256,
        "model_registry_record_sha256": model_entry.record_sha256,
        "strategy_registry_record_sha256": strategy_entry.record_sha256,
        "dataset_snapshot_id": dataset_id,
        "dataset_registry_record_sha256": dataset_entry.record_sha256,
        "feature_set_id": feature_id,
        "feature_registry_record_sha256": feature_entry.record_sha256,
        "research_protocol_id": protocol_id,
        "protocol_registry_record_sha256": protocol_entry.record_sha256,
        "research_question_id": question_id,
        "research_question_registry_record_sha256": question_entry.record_sha256,
        "hypothesis_id": hypothesis_id,
        "hypothesis_registry_record_sha256": hypothesis_entry.record_sha256,
        "config_sha256": model_entry.payload.get("config_sha256"),
        "training_manifest_sha256": model.training_manifest_sha256,
        "uncertainty_semantics": "max-descriptive-rating-radius-not-probability-ci",
    }
    if isinstance(model, HistogramCalibratedStrengthModel):
        provenance["calibration_evidence"] = model.calibration_evidence()

    forecast_identity = _digest(
        {
            "quote_key": quote,
            "decision_at": evidence.decision_at,
            "model_version_id": model_id,
            "strategy_version_id": strategy_id,
            "subject_snapshot_id": evidence.subject.snapshot_id,
            "opponent_snapshot_id": evidence.opponent.snapshot_id,
            "model_identity_sha256": model.identity_sha256,
        }
    )
    return ForecastRecord(
        quote_key=quote,
        probability=probability,
        model_id=model_id,
        model_version=model_id,
        strategy_version=strategy_id,
        model_training_cutoff_ts=model.training_cutoff,
        input_cutoff_ts=evidence.decision_at,
        generated_at=evidence.decision_at,
        uncertainty=uncertainty,
        evidence_hashes=(
            evidence.subject.snapshot_id,
            evidence.opponent.snapshot_id,
            artifact_sha256,
            model_entry.record_sha256,
            strategy_entry.record_sha256,
            dataset_entry.record_sha256,
            feature_entry.record_sha256,
            protocol_entry.record_sha256,
            question_entry.record_sha256,
            hypothesis_entry.record_sha256,
        ),
        provenance=provenance,
        forecast_id=forecast_identity,
    )
