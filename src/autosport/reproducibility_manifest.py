"""Immutable scientific reproducibility-manifest authority.

This module is intentionally authority-only: it defines and validates one canonical
manifest that the existing Strategy/Model Factory can hash-bind into its durable
artifacts.  It does not own model training, evaluation arithmetic, promotion,
holdout spending, execution, or financial authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Mapping, Protocol, Sequence


REPRODUCIBILITY_MANIFEST_KIND: Final = (
    "autosport-factory-reproducibility-manifest-v1"
)
REPRODUCIBILITY_MANIFEST_SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class ReproducibilityManifestError(ValueError):
    """Raised when reproducibility lineage is incomplete or non-canonical."""


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ReproducibilityManifestError(
            f"{name} must be a non-empty canonical string"
        )
    if "\x00" in value:
        raise ReproducibilityManifestError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ReproducibilityManifestError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ReproducibilityManifestError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReproducibilityManifestError(
            f"{name} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReproducibilityManifestError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_digest(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ReproducibilityManifestError(
            "reproducibility manifest payload is not canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    """Exact ordered-input indices used by one causal walk-forward fold."""

    fold_id: str
    training_indices: tuple[int, ...]
    evaluation_index: int
    training_cutoff: str
    evaluation_at: str

    def __post_init__(self) -> None:
        _text("fold_id", self.fold_id)
        if type(self.training_indices) is not tuple or not self.training_indices:
            raise ReproducibilityManifestError(
                "training_indices must be a non-empty tuple"
            )
        for index in self.training_indices:
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ReproducibilityManifestError(
                    "training_indices must contain non-negative integers"
                )
        if self.training_indices != tuple(sorted(self.training_indices)):
            raise ReproducibilityManifestError(
                "training_indices must be strictly canonical ascending order"
            )
        if len(self.training_indices) != len(set(self.training_indices)):
            raise ReproducibilityManifestError(
                "training_indices must not contain duplicates"
            )
        if (
            isinstance(self.evaluation_index, bool)
            or not isinstance(self.evaluation_index, int)
            or self.evaluation_index < 0
        ):
            raise ReproducibilityManifestError(
                "evaluation_index must be a non-negative integer"
            )
        if any(index >= self.evaluation_index for index in self.training_indices):
            raise ReproducibilityManifestError(
                "walk-forward training indices must precede evaluation_index"
            )
        cutoff = _instant("training_cutoff", self.training_cutoff)
        evaluation = _instant("evaluation_at", self.evaluation_at)
        if cutoff >= evaluation:
            raise ReproducibilityManifestError(
                "training_cutoff must strictly precede evaluation_at"
            )

    def to_payload(self) -> dict[str, object]:
        return {
            "fold_id": self.fold_id,
            "training_indices": list(self.training_indices),
            "evaluation_index": self.evaluation_index,
            "training_cutoff": self.training_cutoff,
            "evaluation_at": self.evaluation_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "WalkForwardSplit":
        expected = {
            "fold_id",
            "training_indices",
            "evaluation_index",
            "training_cutoff",
            "evaluation_at",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise ReproducibilityManifestError(
                "walk-forward split fields mismatch"
            )
        raw_indices = payload["training_indices"]
        if type(raw_indices) is not list:
            raise ReproducibilityManifestError(
                "walk-forward training_indices must be a JSON list"
            )
        return cls(
            fold_id=payload["fold_id"],
            training_indices=tuple(raw_indices),
            evaluation_index=payload["evaluation_index"],
            training_cutoff=payload["training_cutoff"],
            evaluation_at=payload["evaluation_at"],
        )


class _TrainingPointEvidence(Protocol):
    observed_at: str

    @property
    def target_reveal_at(self) -> str: ...


class _WalkForwardFoldEvidence(Protocol):
    fold_id: str
    training_cutoff: str
    evaluation_at: str
    causal_training_count: int


def derive_walk_forward_splits(
    points: Sequence[_TrainingPointEvidence],
    folds: Sequence[_WalkForwardFoldEvidence],
    *,
    minimum_train_size: int = 2,
) -> tuple[WalkForwardSplit, ...]:
    """Reconstruct exact evaluator indices from existing causal fold evidence.

    The Strategy/Model Factory orders points by ``observed_at`` and, for each fold,
    admits only prior labels revealed by that fold's training cutoff.  This adapter
    repeats only that indexing rule and rejects any fold witness whose declared
    causal training count cannot be reproduced from the governed inputs.
    """

    if not points:
        raise ReproducibilityManifestError(
            "walk-forward reproducibility requires governed input points"
        )
    if not folds:
        raise ReproducibilityManifestError(
            "walk-forward reproducibility requires fold evidence"
        )
    if (
        isinstance(minimum_train_size, bool)
        or not isinstance(minimum_train_size, int)
        or minimum_train_size < 1
    ):
        raise ReproducibilityManifestError(
            "minimum_train_size must be a positive integer"
        )

    indexed_points = list(enumerate(points))
    for _, point in indexed_points:
        observed = _instant("point observed_at", point.observed_at)
        revealed = _instant("point target_reveal_at", point.target_reveal_at)
        if revealed < observed:
            raise ReproducibilityManifestError(
                "point target_reveal_at must not precede observed_at"
            )
    ordered = sorted(
        indexed_points,
        key=lambda item: _instant("point observed_at", item[1].observed_at),
    )
    ordered_instants = [
        _instant("point observed_at", point.observed_at) for _, point in ordered
    ]
    if len(ordered_instants) != len(set(ordered_instants)):
        raise ReproducibilityManifestError(
            "walk-forward governed inputs require unique observed_at instants"
        )

    split_evidence: list[WalkForwardSplit] = []
    for fold in folds:
        fold_evaluation = _instant("fold evaluation_at", fold.evaluation_at)
        matching_indices = [
            index
            for index, (_, point) in enumerate(ordered)
            if _instant("point observed_at", point.observed_at) == fold_evaluation
        ]
        if len(matching_indices) != 1:
            raise ReproducibilityManifestError(
                "fold evaluation_at does not resolve to one governed input index"
            )
        evaluation_index = matching_indices[0]
        if evaluation_index == 0:
            raise ReproducibilityManifestError(
                "walk-forward fold cannot evaluate the first governed input"
            )
        cutoff = _instant("fold training_cutoff", fold.training_cutoff)
        expected_cutoff = _instant(
            "prior point observed_at", ordered[evaluation_index - 1][1].observed_at
        )
        if cutoff != expected_cutoff:
            raise ReproducibilityManifestError(
                "fold training_cutoff does not match preceding governed input"
            )
        if (
            isinstance(fold.causal_training_count, bool)
            or not isinstance(fold.causal_training_count, int)
            or fold.causal_training_count <= 0
        ):
            raise ReproducibilityManifestError(
                "fold causal_training_count must be a positive integer"
            )
        training_indices = tuple(
            index
            for index, (_, point) in enumerate(ordered[:evaluation_index])
            if _instant("point observed_at", point.observed_at) <= cutoff
            and _instant("point target_reveal_at", point.target_reveal_at) <= cutoff
        )
        if len(training_indices) != fold.causal_training_count:
            raise ReproducibilityManifestError(
                "fold causal_training_count does not match governed input lineage"
            )
        split_evidence.append(
            WalkForwardSplit(
                fold_id=fold.fold_id,
                training_indices=training_indices,
                evaluation_index=evaluation_index,
                training_cutoff=fold.training_cutoff,
                evaluation_at=fold.evaluation_at,
            )
        )
    evaluation_indices = tuple(
        split.evaluation_index for split in split_evidence
    )
    if evaluation_indices != tuple(sorted(set(evaluation_indices))):
        raise ReproducibilityManifestError(
            "fold evidence must be unique and ordered by evaluation_index"
        )
    canonical_evaluation_indices: list[int] = []
    for evaluation_index in range(1, len(ordered)):
        cutoff = _instant(
            "prior point observed_at",
            ordered[evaluation_index - 1][1].observed_at,
        )
        causal_training_count = sum(
            1
            for _, point in ordered[:evaluation_index]
            if _instant("point observed_at", point.observed_at) <= cutoff
            and _instant("point target_reveal_at", point.target_reveal_at) <= cutoff
        )
        if causal_training_count >= minimum_train_size:
            canonical_evaluation_indices.append(evaluation_index)

    expected_evaluation_indices = tuple(canonical_evaluation_indices)
    if not expected_evaluation_indices:
        raise ReproducibilityManifestError(
            "governed inputs do not produce a canonical walk-forward fold "
            "under minimum_train_size"
        )
    if evaluation_indices != expected_evaluation_indices:
        raise ReproducibilityManifestError(
            "fold evidence must match complete canonical evaluation coverage "
            "from the first admissible fold through the final governed input"
        )
    return tuple(split_evidence)


@dataclass(frozen=True, slots=True)
class FactoryReproducibilityManifest:
    """Hash-stable lineage required to reproduce one governed factory result.

    The manifest deliberately stores both the frozen DatasetSnapshot manifest and the
    exact factory input-population manifest and requires them to be identical.  Exact
    split indices are relative to that canonical ordered input population.
    """

    experiment_id: str
    evaluation_bundle_id: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    training_points_manifest_sha256: str
    dataset_source_identity: str
    dataset_license_identity: str
    splits: tuple[WalkForwardSplit, ...]
    model_version_id: str
    model_artifact_sha256: str
    learner_state_sha256: str
    model_config_sha256: str
    research_protocol_id: str
    protocol_sha256: str
    evaluator_config_sha256: str
    source_sha256: str
    evaluator_source_sha256: str
    environment_sha256: str
    seed: int

    def __post_init__(self) -> None:
        for name in (
            "experiment_id",
            "evaluation_bundle_id",
            "dataset_snapshot_id",
            "dataset_source_identity",
            "dataset_license_identity",
            "model_version_id",
            "research_protocol_id",
        ):
            _text(name, getattr(self, name))
        for name in (
            "dataset_manifest_sha256",
            "training_points_manifest_sha256",
            "model_artifact_sha256",
            "learner_state_sha256",
            "model_config_sha256",
            "protocol_sha256",
            "evaluator_config_sha256",
            "source_sha256",
            "evaluator_source_sha256",
            "environment_sha256",
        ):
            _sha256(name, getattr(self, name))
        if self.dataset_manifest_sha256 != self.training_points_manifest_sha256:
            raise ReproducibilityManifestError(
                "factory input manifest must match frozen DatasetSnapshot manifest"
            )
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ReproducibilityManifestError("seed must be an integer")
        if type(self.splits) is not tuple or not self.splits:
            raise ReproducibilityManifestError(
                "splits must be a non-empty tuple of WalkForwardSplit values"
            )
        if any(not isinstance(split, WalkForwardSplit) for split in self.splits):
            raise ReproducibilityManifestError(
                "splits must contain WalkForwardSplit values"
            )
        fold_ids = tuple(split.fold_id for split in self.splits)
        if len(fold_ids) != len(set(fold_ids)):
            raise ReproducibilityManifestError("split fold_id values must be unique")
        evaluation_indices = tuple(split.evaluation_index for split in self.splits)
        if len(evaluation_indices) != len(set(evaluation_indices)):
            raise ReproducibilityManifestError(
                "split evaluation_index values must be unique"
            )
        if evaluation_indices != tuple(sorted(evaluation_indices)):
            raise ReproducibilityManifestError(
                "splits must be ordered by evaluation_index"
            )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": REPRODUCIBILITY_MANIFEST_SCHEMA_VERSION,
            "kind": REPRODUCIBILITY_MANIFEST_KIND,
            "experiment_id": self.experiment_id,
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "dataset": {
                "dataset_snapshot_id": self.dataset_snapshot_id,
                "dataset_manifest_sha256": self.dataset_manifest_sha256,
                "training_points_manifest_sha256": self.training_points_manifest_sha256,
                "input_count": self.splits[-1].evaluation_index + 1,
                "source_identity": self.dataset_source_identity,
                "license_identity": self.dataset_license_identity,
            },
            "walk_forward_splits": [split.to_payload() for split in self.splits],
            "model": {
                "model_version_id": self.model_version_id,
                "model_artifact_sha256": self.model_artifact_sha256,
                "learner_state_sha256": self.learner_state_sha256,
                "config_sha256": self.model_config_sha256,
                "seed": self.seed,
            },
            "research": {
                "research_protocol_id": self.research_protocol_id,
                "protocol_sha256": self.protocol_sha256,
                "evaluator_config_sha256": self.evaluator_config_sha256,
            },
            "software_environment": {
                "source_sha256": self.source_sha256,
                "evaluator_source_sha256": self.evaluator_source_sha256,
                "environment_sha256": self.environment_sha256,
            },
            "truth": {
                "promotion_claim": False,
                "real_money_execution": False,
            },
        }

    @property
    def manifest_sha256(self) -> str:
        return _canonical_digest(self.canonical_payload())

    def to_envelope(self) -> dict[str, object]:
        payload = self.canonical_payload()
        return {**payload, "manifest_sha256": self.manifest_sha256}

    @classmethod
    def from_envelope(cls, envelope: object) -> "FactoryReproducibilityManifest":
        expected = {
            "schema_version",
            "kind",
            "experiment_id",
            "evaluation_bundle_id",
            "dataset",
            "walk_forward_splits",
            "model",
            "research",
            "software_environment",
            "truth",
            "manifest_sha256",
        }
        if type(envelope) is not dict or set(envelope) != expected:
            raise ReproducibilityManifestError(
                "reproducibility manifest envelope fields mismatch"
            )
        if envelope["schema_version"] != REPRODUCIBILITY_MANIFEST_SCHEMA_VERSION:
            raise ReproducibilityManifestError(
                "unsupported reproducibility manifest schema_version"
            )
        if envelope["kind"] != REPRODUCIBILITY_MANIFEST_KIND:
            raise ReproducibilityManifestError(
                "unsupported reproducibility manifest kind"
            )

        dataset = envelope["dataset"]
        model = envelope["model"]
        research = envelope["research"]
        software = envelope["software_environment"]
        truth = envelope["truth"]
        if type(dataset) is not dict or set(dataset) != {
            "dataset_snapshot_id",
            "dataset_manifest_sha256",
            "training_points_manifest_sha256",
            "input_count",
            "source_identity",
            "license_identity",
        }:
            raise ReproducibilityManifestError("dataset lineage fields mismatch")
        if type(model) is not dict or set(model) != {
            "model_version_id",
            "model_artifact_sha256",
            "learner_state_sha256",
            "config_sha256",
            "seed",
        }:
            raise ReproducibilityManifestError("model lineage fields mismatch")
        if type(research) is not dict or set(research) != {
            "research_protocol_id",
            "protocol_sha256",
            "evaluator_config_sha256",
        }:
            raise ReproducibilityManifestError("research lineage fields mismatch")
        if type(software) is not dict or set(software) != {
            "source_sha256",
            "evaluator_source_sha256",
            "environment_sha256",
        }:
            raise ReproducibilityManifestError(
                "software/environment lineage fields mismatch"
            )
        if truth != {
            "promotion_claim": False,
            "real_money_execution": False,
        }:
            raise ReproducibilityManifestError(
                "reproducibility manifest truth boundary mismatch"
            )
        raw_splits = envelope["walk_forward_splits"]
        if type(raw_splits) is not list or not raw_splits:
            raise ReproducibilityManifestError(
                "walk_forward_splits must be a non-empty JSON list"
            )
        manifest = cls(
            experiment_id=envelope["experiment_id"],
            evaluation_bundle_id=envelope["evaluation_bundle_id"],
            dataset_snapshot_id=dataset["dataset_snapshot_id"],
            dataset_manifest_sha256=dataset["dataset_manifest_sha256"],
            training_points_manifest_sha256=dataset[
                "training_points_manifest_sha256"
            ],
            dataset_source_identity=dataset["source_identity"],
            dataset_license_identity=dataset["license_identity"],
            splits=tuple(WalkForwardSplit.from_payload(raw) for raw in raw_splits),
            model_version_id=model["model_version_id"],
            model_artifact_sha256=model["model_artifact_sha256"],
            learner_state_sha256=model["learner_state_sha256"],
            model_config_sha256=model["config_sha256"],
            research_protocol_id=research["research_protocol_id"],
            protocol_sha256=research["protocol_sha256"],
            evaluator_config_sha256=research["evaluator_config_sha256"],
            source_sha256=software["source_sha256"],
            evaluator_source_sha256=software["evaluator_source_sha256"],
            environment_sha256=software["environment_sha256"],
            seed=model["seed"],
        )
        input_count = dataset["input_count"]
        if (
            isinstance(input_count, bool)
            or not isinstance(input_count, int)
            or input_count != manifest.splits[-1].evaluation_index + 1
        ):
            raise ReproducibilityManifestError(
                "dataset input_count does not match split lineage"
            )
        if _sha256(
            "manifest_sha256", envelope["manifest_sha256"]
        ) != manifest.manifest_sha256:
            raise ReproducibilityManifestError(
                "reproducibility manifest digest mismatch"
            )
        return manifest
