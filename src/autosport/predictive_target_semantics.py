"""Preregistered predictive-target semantics for factory training populations.

This module closes one narrow scientific provenance gap: a finite numeric
``TrainingPoint.target`` is not, by itself, evidence that the target is a
Bernoulli outcome indicator or that a fitted model's output is an outcome
probability.

The contract is deliberately additive.  It reuses the canonical
ScientificProtocolBinding.expected_artifacts commitment, the existing
ScientificRegistry ResearchProtocol, the factory training-points manifest,
and the factory reproducibility manifest.  It does not create another
registry, model, evaluation engine, forecast ledger, or promotion authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Sequence

from ._strategy_model_factory_impl import (
    TrainingPoint,
    training_points_manifest_sha256,
)
from .reproducibility_manifest import FactoryReproducibilityManifest
from .scientific_registry import ScientificRegistry


PREDICTIVE_TARGET_CONTRACT_KIND: Final = (
    "autosport-predictive-target-contract-v1"
)
PREDICTIVE_TARGET_CONTRACT_SCHEMA_VERSION: Final = 1
BINARY_OUTCOME_TARGET_KIND: Final = "bernoulli-outcome-indicator-v1"
BINARY_PROBABILITY_INTERPRETATION: Final = "bernoulli-event-probability-v1"
PREDICTIVE_TARGET_ARTIFACT_PREFIX: Final = (
    "predictive-target-contract-sha256:"
)
_HEX: Final = frozenset("0123456789abcdef")


class PredictiveTargetSemanticsError(ValueError):
    """Raised when a predictive-target provenance contract cannot be proven."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PredictiveTargetSemanticsError(
            f"{name} must be a non-empty canonical string"
        )
    if "\x00" in value:
        raise PredictiveTargetSemanticsError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PredictiveTargetSemanticsError(
            f"{name} must be valid UTF-8"
        ) from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise PredictiveTargetSemanticsError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PredictiveTargetSemanticsError(
            f"{name} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PredictiveTargetSemanticsError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_digest(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PredictiveTargetSemanticsError(
            "predictive target payload is not canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PredictiveTargetContract:
    """Hash-stable preregistration for one binary supervised target.

    ``target_semantics_id`` identifies the externally defined scientific
    quantity.  The two definition digests bind the exact positive/negative
    outcome rules without copying settlement/outcome logic into this module.
    """

    contract_id: str
    research_protocol_id: str
    target_semantics_id: str
    positive_outcome_definition_sha256: str
    negative_outcome_definition_sha256: str
    frozen_at: str

    def __post_init__(self) -> None:
        for name in (
            "contract_id",
            "research_protocol_id",
            "target_semantics_id",
        ):
            _text(getattr(self, name), name)
        _sha256(
            self.positive_outcome_definition_sha256,
            "positive_outcome_definition_sha256",
        )
        _sha256(
            self.negative_outcome_definition_sha256,
            "negative_outcome_definition_sha256",
        )
        if (
            self.positive_outcome_definition_sha256
            == self.negative_outcome_definition_sha256
        ):
            raise PredictiveTargetSemanticsError(
                "positive and negative outcome definitions must differ"
            )
        _instant(self.frozen_at, "frozen_at")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": PREDICTIVE_TARGET_CONTRACT_SCHEMA_VERSION,
            "kind": PREDICTIVE_TARGET_CONTRACT_KIND,
            "contract_id": self.contract_id,
            "research_protocol_id": self.research_protocol_id,
            "target_semantics_id": self.target_semantics_id,
            "target_kind": BINARY_OUTCOME_TARGET_KIND,
            "target_value_domain": [0, 1],
            "prediction_interpretation": BINARY_PROBABILITY_INTERPRETATION,
            "positive_outcome_definition_sha256": (
                self.positive_outcome_definition_sha256
            ),
            "negative_outcome_definition_sha256": (
                self.negative_outcome_definition_sha256
            ),
            "frozen_at": self.frozen_at,
        }

    @property
    def contract_sha256(self) -> str:
        return _canonical_digest(self.canonical_payload())

    @property
    def preregistration_token(self) -> str:
        return PREDICTIVE_TARGET_ARTIFACT_PREFIX + self.contract_sha256


@dataclass(frozen=True, slots=True)
class PreregisteredBinaryTargetPopulation:
    """Audit evidence for a preregistered binary-valued training population.

    This evidence proves that one exact binary-valued population and its
    evidence digests were precommitted by the frozen ResearchProtocol.  It
    does *not* prove that those evidence digests correctly classify the
    external-world outcome under the referenced definitions.  It also does
    not prove model-output probability semantics, calibration, Forecast
    authority, promotion, allocation, or execution.
    """

    contract_id: str
    contract_sha256: str
    target_semantics_id: str
    research_protocol_id: str
    research_protocol_record_sha256: str
    protocol_sha256: str
    reproducibility_manifest_sha256: str
    training_points_manifest_sha256: str
    target_population_sha256: str
    input_count: int
    positive_count: int
    negative_count: int
    resolved_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "autosport-preregistered-binary-target-population-v1",
            "contract_id": self.contract_id,
            "contract_sha256": self.contract_sha256,
            "target_semantics_id": self.target_semantics_id,
            "research_protocol_id": self.research_protocol_id,
            "research_protocol_record_sha256": (
                self.research_protocol_record_sha256
            ),
            "protocol_sha256": self.protocol_sha256,
            "reproducibility_manifest_sha256": (
                self.reproducibility_manifest_sha256
            ),
            "training_points_manifest_sha256": (
                self.training_points_manifest_sha256
            ),
            "target_population_sha256": self.target_population_sha256,
            "input_count": self.input_count,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "resolved_at": self.resolved_at,
            "truth": {
                "target_contract_preregistered": True,
                "binary_value_domain_verified": True,
                "training_population_hash_bound": True,
                "outcome_label_semantics_verified": False,
                "model_output_probability_authority": False,
                "calibration_authority": False,
                "forecast_authority": False,
                "promotion_authority": False,
                "real_money_execution": False,
            },
        }


def _ordered_points(
    points: Sequence[TrainingPoint],
) -> tuple[TrainingPoint, ...]:
    if not points:
        raise PredictiveTargetSemanticsError(
            "predictive target resolution requires training points"
        )
    if any(type(point) is not TrainingPoint for point in points):
        raise PredictiveTargetSemanticsError(
            "training points must be exact canonical TrainingPoint values"
        )
    ordered = tuple(
        sorted(
            points,
            key=lambda point: _instant(
                point.observed_at, "training point observed_at"
            ),
        )
    )
    observed = tuple(
        _instant(point.observed_at, "training point observed_at")
        for point in ordered
    )
    if len(observed) != len(set(observed)):
        raise PredictiveTargetSemanticsError(
            "training points require unique observed_at instants"
        )
    return ordered


def _target_population_digest(
    *,
    contract_sha256: str,
    training_manifest_sha256: str,
    ordered_points: Sequence[TrainingPoint],
) -> str:
    return _canonical_digest(
        {
            "schema_version": 1,
            "kind": "autosport-binary-target-population-v1",
            "contract_sha256": contract_sha256,
            "training_points_manifest_sha256": training_manifest_sha256,
            "targets": [
                {
                    "observed_at": point.observed_at,
                    "target": int(point.target),
                    "target_available_at": point.target_reveal_at,
                    "evidence_sha256s": list(point.evidence_sha256s),
                }
                for point in ordered_points
            ],
        }
    )


def resolve_preregistered_binary_target_population(
    registry: ScientificRegistry,
    manifest: FactoryReproducibilityManifest,
    contract: PredictiveTargetContract,
    points: Sequence[TrainingPoint],
    *,
    as_of: str,
) -> PreregisteredBinaryTargetPopulation:
    """Resolve a precommitted binary-valued target population.

    Positive resolution requires the contract digest to have been frozen into
    the exact ResearchProtocol's ``expected_artifacts`` before the governed
    training population begins.  Every target must be exactly 0/1 and must
    carry at least one causal evidence digest.  This function intentionally
    does not verify the external semantic correctness of those label-evidence
    digests and therefore cannot authorize a probability forecast by itself.
    """

    if type(registry) is not ScientificRegistry:
        raise PredictiveTargetSemanticsError(
            "registry must be exact canonical ScientificRegistry"
        )
    if type(manifest) is not FactoryReproducibilityManifest:
        raise PredictiveTargetSemanticsError(
            "manifest must be exact canonical FactoryReproducibilityManifest"
        )
    if type(contract) is not PredictiveTargetContract:
        raise PredictiveTargetSemanticsError(
            "contract must be exact canonical PredictiveTargetContract"
        )

    resolved_at = _instant(as_of, "as_of")
    ordered = _ordered_points(points)
    first_observed = _instant(
        ordered[0].observed_at, "first training point observed_at"
    )

    if contract.research_protocol_id != manifest.research_protocol_id:
        raise PredictiveTargetSemanticsError(
            "predictive target contract research protocol mismatch"
        )

    try:
        actual_manifest = training_points_manifest_sha256(points)
    except (TypeError, ValueError) as exc:
        raise PredictiveTargetSemanticsError(
            "training points cannot be canonicalized by factory authority"
        ) from exc
    if actual_manifest != manifest.training_points_manifest_sha256:
        raise PredictiveTargetSemanticsError(
            "training points do not match reproducibility manifest"
        )
    if actual_manifest != manifest.dataset_manifest_sha256:
        raise PredictiveTargetSemanticsError(
            "training points do not match frozen dataset manifest"
        )
    expected_count = manifest.splits[-1].evaluation_index + 1
    if len(ordered) != expected_count:
        raise PredictiveTargetSemanticsError(
            "training point count does not match reproducibility split lineage"
        )

    protocol = registry.get(
        "ResearchProtocol", manifest.research_protocol_id
    )
    if protocol is None:
        raise PredictiveTargetSemanticsError(
            "reproducibility ResearchProtocol is absent from ScientificRegistry"
        )
    protocol_sha256 = protocol.payload.get("protocol_sha256")
    if protocol_sha256 != manifest.protocol_sha256:
        raise PredictiveTargetSemanticsError(
            "ResearchProtocol digest does not match reproducibility manifest"
        )
    if _instant(protocol.available_at, "ResearchProtocol available_at") > (
        first_observed
    ):
        raise PredictiveTargetSemanticsError(
            "ResearchProtocol was not durably available before training population"
        )
    if _instant(protocol.available_at, "ResearchProtocol available_at") > (
        resolved_at
    ):
        raise PredictiveTargetSemanticsError(
            "ResearchProtocol is not causally available at resolution time"
        )

    binding = protocol.payload.get("binding")
    if type(binding) is not dict:
        raise PredictiveTargetSemanticsError(
            "ResearchProtocol binding is not canonical"
        )
    if binding.get("research_protocol_id") != contract.research_protocol_id:
        raise PredictiveTargetSemanticsError(
            "ResearchProtocol binding identity does not match target contract"
        )
    protocol_frozen = _instant(
        binding.get("frozen_at_utc"), "ResearchProtocol frozen_at_utc"
    )
    if protocol_frozen > first_observed:
        raise PredictiveTargetSemanticsError(
            "ResearchProtocol was frozen after training population began"
        )
    if _instant(contract.frozen_at, "target contract frozen_at") > (
        protocol_frozen
    ):
        raise PredictiveTargetSemanticsError(
            "predictive target contract was not frozen by protocol freeze"
        )

    expected_artifacts = binding.get("expected_artifacts")
    if type(expected_artifacts) is not list:
        raise PredictiveTargetSemanticsError(
            "ResearchProtocol expected_artifacts are invalid"
        )
    target_tokens = tuple(
        value
        for value in expected_artifacts
        if type(value) is str
        and value.startswith(PREDICTIVE_TARGET_ARTIFACT_PREFIX)
    )
    if len(target_tokens) != 1:
        raise PredictiveTargetSemanticsError(
            "ResearchProtocol must preregister exactly one predictive target contract"
        )
    if target_tokens[0] != contract.preregistration_token:
        raise PredictiveTargetSemanticsError(
            "predictive target contract digest does not match preregistration"
        )

    positive_count = 0
    negative_count = 0
    for point in ordered:
        if type(point.target) not in (int, float) or point.target not in (
            0,
            1,
            0.0,
            1.0,
        ):
            raise PredictiveTargetSemanticsError(
                "binary target values must be exactly 0 or 1"
            )
        if not point.evidence_sha256s:
            raise PredictiveTargetSemanticsError(
                "binary target labels require causal evidence digests"
            )
        reveal = _instant(
            point.target_reveal_at, "training point target_available_at"
        )
        observed = _instant(
            point.observed_at, "training point observed_at"
        )
        if reveal < observed:
            raise PredictiveTargetSemanticsError(
                "target reveal cannot precede observation"
            )
        if reveal > resolved_at:
            raise PredictiveTargetSemanticsError(
                "target label was not causally available at resolution time"
            )
        if point.target in (1, 1.0):
            positive_count += 1
        else:
            negative_count += 1

    target_population_sha256 = _target_population_digest(
        contract_sha256=contract.contract_sha256,
        training_manifest_sha256=actual_manifest,
        ordered_points=ordered,
    )
    return PreregisteredBinaryTargetPopulation(
        contract_id=contract.contract_id,
        contract_sha256=contract.contract_sha256,
        target_semantics_id=contract.target_semantics_id,
        research_protocol_id=manifest.research_protocol_id,
        research_protocol_record_sha256=_sha256(
            protocol.record_sha256, "ResearchProtocol record_sha256"
        ),
        protocol_sha256=_sha256(
            manifest.protocol_sha256, "protocol_sha256"
        ),
        reproducibility_manifest_sha256=manifest.manifest_sha256,
        training_points_manifest_sha256=actual_manifest,
        target_population_sha256=target_population_sha256,
        input_count=len(ordered),
        positive_count=positive_count,
        negative_count=negative_count,
        resolved_at=as_of,
    )
