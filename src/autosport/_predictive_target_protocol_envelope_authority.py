"""Bind predictive-target provenance to the exact durable ResearchProtocol envelope.

This is an additive hardening layer over the canonical factory reproducibility
manifest and predictive-target resolver.  It does not create a second registry,
model, evaluator, forecast ledger, or promotion authority.
"""

from __future__ import annotations

import copy
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from typing import Any, Sequence

from . import _scientific_registry_read_authority as _read_authority
from . import _strategy_model_factory_impl as _factory
from . import predictive_target_semantics as _target
from . import reproducibility_manifest as _manifest
from . import scientific_registry as _registry


_BASE_MANIFEST = _manifest.FactoryReproducibilityManifest
_PROTOCOL_ENVELOPE_CONTEXT: ContextVar[tuple[str, str] | None] = ContextVar(
    "autosport_factory_protocol_envelope",
    default=None,
)


def _context_record_sha256() -> str | None:
    value = _PROTOCOL_ENVELOPE_CONTEXT.get()
    return None if value is None else value[0]


def _context_available_at() -> str | None:
    value = _PROTOCOL_ENVELOPE_CONTEXT.get()
    return None if value is None else value[1]


@dataclass(frozen=True, slots=True)
class BoundFactoryReproducibilityManifest(_BASE_MANIFEST):
    """Factory manifest with optional exact ResearchProtocol envelope identity.

    Legacy/direct manifests remain byte-compatible when both additive fields are
    absent.  Factory-produced manifests created under the installed runner guard
    carry both values and are the only manifests eligible for predictive-target
    authority.
    """

    research_protocol_record_sha256: str | None = field(
        default_factory=_context_record_sha256
    )
    research_protocol_available_at: str | None = field(
        default_factory=_context_available_at
    )

    def __post_init__(self) -> None:
        _BASE_MANIFEST.__post_init__(self)
        values = (
            self.research_protocol_record_sha256,
            self.research_protocol_available_at,
        )
        if (values[0] is None) != (values[1] is None):
            raise _manifest.ReproducibilityManifestError(
                "ResearchProtocol envelope binding must be complete"
            )
        if values[0] is not None:
            _manifest._sha256(
                "research_protocol_record_sha256",
                values[0],
            )
            _manifest._instant(
                "research_protocol_available_at",
                values[1],
            )

    def canonical_payload(self) -> dict[str, object]:
        payload = _BASE_MANIFEST.canonical_payload(self)
        if self.research_protocol_record_sha256 is not None:
            research = payload["research"]
            if type(research) is not dict:
                raise _manifest.ReproducibilityManifestError(
                    "research lineage payload is invalid"
                )
            research["research_protocol_record_sha256"] = (
                self.research_protocol_record_sha256
            )
            research["research_protocol_available_at"] = (
                self.research_protocol_available_at
            )
        return payload

    @classmethod
    def from_envelope(
        cls,
        envelope: object,
    ) -> _BASE_MANIFEST:
        if type(envelope) is not dict:
            raise _manifest.ReproducibilityManifestError(
                "reproducibility manifest envelope fields mismatch"
            )
        research = envelope.get("research")
        if type(research) is not dict:
            raise _manifest.ReproducibilityManifestError(
                "research lineage fields mismatch"
            )
        additive = {
            "research_protocol_record_sha256",
            "research_protocol_available_at",
        }
        present = additive.intersection(research)
        if not present:
            return _BASE_MANIFEST.from_envelope(envelope)
        if present != additive:
            raise _manifest.ReproducibilityManifestError(
                "ResearchProtocol envelope binding must be complete"
            )

        legacy = copy.deepcopy(envelope)
        legacy_research = legacy["research"]
        if type(legacy_research) is not dict:
            raise _manifest.ReproducibilityManifestError(
                "research lineage fields mismatch"
            )
        record_sha256 = legacy_research.pop(
            "research_protocol_record_sha256"
        )
        available_at = legacy_research.pop("research_protocol_available_at")
        legacy_payload = {
            key: value
            for key, value in legacy.items()
            if key != "manifest_sha256"
        }
        legacy["manifest_sha256"] = _manifest._canonical_digest(legacy_payload)
        base = _BASE_MANIFEST.from_envelope(legacy)
        base_values = {
            item.name: getattr(base, item.name)
            for item in fields(_BASE_MANIFEST)
        }
        result = cls(
            **base_values,
            research_protocol_record_sha256=record_sha256,
            research_protocol_available_at=available_at,
        )
        supplied_digest = _manifest._sha256(
            "manifest_sha256",
            envelope.get("manifest_sha256"),
        )
        if supplied_digest != result.manifest_sha256:
            raise _manifest.ReproducibilityManifestError(
                "reproducibility manifest digest mismatch"
            )
        return result


def _instance_dispatch_is_shadowed(registry: _registry.ScientificRegistry) -> bool:
    namespace = getattr(registry, "__dict__", None)
    return type(namespace) is dict and (
        "get" in namespace or "_read" in namespace
    )


def _verified_protocol_raw(
    registry: _registry.ScientificRegistry,
    research_protocol_id: str,
) -> dict[str, Any]:
    if type(registry) is not _registry.ScientificRegistry:
        raise _target.PredictiveTargetSemanticsError(
            "registry must be exact canonical ScientificRegistry"
        )
    if _instance_dispatch_is_shadowed(registry):
        raise _target.PredictiveTargetSemanticsError(
            "ScientificRegistry instance read dispatch is shadowed"
        )
    try:
        read_fn, _validate_fn, _get_fn, _causal_fn = (
            _read_authority.require_scientific_registry_read_authority()
        )
        state = read_fn(registry)
    except _read_authority.ScientificRegistryReadAuthorityError as exc:
        raise _target.PredictiveTargetSemanticsError(
            "ScientificRegistry read authority is not canonical"
        ) from exc
    matches = [
        raw
        for raw in state["records"]
        if raw["record_type"] == "ResearchProtocol"
        and raw["record_id"] == research_protocol_id
    ]
    if len(matches) != 1:
        raise _target.PredictiveTargetSemanticsError(
            "reproducibility ResearchProtocol is absent or ambiguous in ScientificRegistry"
        )
    raw = matches[0]
    if type(raw) is not dict:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol registry envelope is invalid"
        )
    return raw


def _require_bound_protocol_entry(
    registry: _registry.ScientificRegistry,
    manifest: BoundFactoryReproducibilityManifest,
) -> dict[str, Any]:
    record_sha256 = manifest.research_protocol_record_sha256
    available_at = manifest.research_protocol_available_at
    if record_sha256 is None or available_at is None:
        raise _target.PredictiveTargetSemanticsError(
            "reproducibility manifest lacks precommitted ResearchProtocol envelope authority"
        )
    raw = _verified_protocol_raw(registry, manifest.research_protocol_id)
    if raw.get("record_sha256") != record_sha256:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol registry record identity does not match reproducibility manifest"
        )
    if raw.get("available_at") != available_at:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol durable availability does not match reproducibility manifest"
        )
    payload = raw.get("payload")
    if type(payload) is not dict:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol payload is invalid"
        )
    if payload.get("protocol_sha256") != manifest.protocol_sha256:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol digest does not match reproducibility manifest"
        )
    return raw


def _resolve_preregistered_binary_target_population(
    registry: _registry.ScientificRegistry,
    manifest: BoundFactoryReproducibilityManifest,
    contract: _target.PredictiveTargetContract,
    points: Sequence[_factory.TrainingPoint],
    *,
    as_of: str,
) -> _target.PreregisteredBinaryTargetPopulation:
    """Resolve target provenance only from a precommitted durable protocol envelope."""

    if type(manifest) is not BoundFactoryReproducibilityManifest:
        raise _target.PredictiveTargetSemanticsError(
            "manifest must be exact canonical bound FactoryReproducibilityManifest"
        )
    if type(contract) is not _target.PredictiveTargetContract:
        raise _target.PredictiveTargetSemanticsError(
            "contract must be exact canonical PredictiveTargetContract"
        )

    resolved_at = _target._instant(as_of, "as_of")
    ordered = _target._ordered_points(points)
    first_observed = _target._instant(
        ordered[0].observed_at,
        "first training point observed_at",
    )
    if contract.research_protocol_id != manifest.research_protocol_id:
        raise _target.PredictiveTargetSemanticsError(
            "predictive target contract research protocol mismatch"
        )

    try:
        actual_manifest = _factory.training_points_manifest_sha256(points)
    except (TypeError, ValueError) as exc:
        raise _target.PredictiveTargetSemanticsError(
            "training points cannot be canonicalized by factory authority"
        ) from exc
    if actual_manifest != manifest.training_points_manifest_sha256:
        raise _target.PredictiveTargetSemanticsError(
            "training points do not match reproducibility manifest"
        )
    if actual_manifest != manifest.dataset_manifest_sha256:
        raise _target.PredictiveTargetSemanticsError(
            "training points do not match frozen dataset manifest"
        )
    expected_count = manifest.splits[-1].evaluation_index + 1
    if len(ordered) != expected_count:
        raise _target.PredictiveTargetSemanticsError(
            "training point count does not match reproducibility split lineage"
        )

    protocol = _require_bound_protocol_entry(registry, manifest)
    protocol_available_at = _target._instant(
        protocol["available_at"],
        "ResearchProtocol available_at",
    )
    if protocol_available_at > first_observed:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol was not durably available before training population"
        )
    if protocol_available_at > resolved_at:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol is not causally available at resolution time"
        )

    payload = protocol["payload"]
    binding = payload.get("binding")
    if type(binding) is not dict:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol binding is not canonical"
        )
    if binding.get("research_protocol_id") != contract.research_protocol_id:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol binding identity does not match target contract"
        )
    protocol_frozen = _target._instant(
        binding.get("frozen_at_utc"),
        "ResearchProtocol frozen_at_utc",
    )
    if protocol_frozen > first_observed:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol was frozen after training population began"
        )
    if _target._instant(
        contract.frozen_at,
        "target contract frozen_at",
    ) > protocol_frozen:
        raise _target.PredictiveTargetSemanticsError(
            "predictive target contract was not frozen by protocol freeze"
        )

    expected_artifacts = binding.get("expected_artifacts")
    if type(expected_artifacts) is not list:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol expected_artifacts are invalid"
        )
    target_tokens = tuple(
        value
        for value in expected_artifacts
        if type(value) is str
        and value.startswith(_target.PREDICTIVE_TARGET_ARTIFACT_PREFIX)
    )
    if len(target_tokens) != 1:
        raise _target.PredictiveTargetSemanticsError(
            "ResearchProtocol must preregister exactly one predictive target contract"
        )
    if target_tokens[0] != contract.preregistration_token:
        raise _target.PredictiveTargetSemanticsError(
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
            raise _target.PredictiveTargetSemanticsError(
                "binary target values must be exactly 0 or 1"
            )
        if not point.evidence_sha256s:
            raise _target.PredictiveTargetSemanticsError(
                "binary target labels require causal evidence digests"
            )
        reveal = _target._instant(
            point.target_reveal_at,
            "training point target_available_at",
        )
        observed = _target._instant(
            point.observed_at,
            "training point observed_at",
        )
        if reveal < observed:
            raise _target.PredictiveTargetSemanticsError(
                "target reveal cannot precede observation"
            )
        if reveal > resolved_at:
            raise _target.PredictiveTargetSemanticsError(
                "target label was not causally available at resolution time"
            )
        if point.target in (1, 1.0):
            positive_count += 1
        else:
            negative_count += 1

    target_population_sha256 = _target._target_population_digest(
        contract_sha256=contract.contract_sha256,
        training_manifest_sha256=actual_manifest,
        ordered_points=ordered,
    )
    return _target.PreregisteredBinaryTargetPopulation(
        contract_id=contract.contract_id,
        contract_sha256=contract.contract_sha256,
        target_semantics_id=contract.target_semantics_id,
        research_protocol_id=manifest.research_protocol_id,
        research_protocol_record_sha256=_target._sha256(
            manifest.research_protocol_record_sha256,
            "ResearchProtocol record_sha256",
        ),
        protocol_sha256=_target._sha256(
            manifest.protocol_sha256,
            "protocol_sha256",
        ),
        reproducibility_manifest_sha256=manifest.manifest_sha256,
        training_points_manifest_sha256=actual_manifest,
        target_population_sha256=target_population_sha256,
        input_count=len(ordered),
        positive_count=positive_count,
        negative_count=negative_count,
        resolved_at=as_of,
    )


def _install_factory_protocol_context() -> None:
    runner = _factory.ExperimentRunner
    if getattr(runner, "_autosport_protocol_envelope_binding_installed", False):
        return
    original = runner.run_baseline_candidate

    def run_baseline_candidate(self, spec, points, *, rule, minimum_train_size=None):
        raw = _verified_protocol_raw(self.registry, spec.research_protocol_id)
        token = _PROTOCOL_ENVELOPE_CONTEXT.set(
            (raw["record_sha256"], raw["available_at"])
        )
        try:
            return original(
                self,
                spec,
                points,
                rule=rule,
                minimum_train_size=minimum_train_size,
            )
        finally:
            _PROTOCOL_ENVELOPE_CONTEXT.reset(token)

    runner._autosport_protocol_envelope_binding_original_run = original
    runner.run_baseline_candidate = run_baseline_candidate
    runner._autosport_protocol_envelope_binding_installed = True


# Replace only the canonical manifest class aliases used by the owning factory,
# parser and target resolver.  Legacy envelopes are still parsed by the captured
# base class, while new factory construction receives the verified context fields.
_manifest.FactoryReproducibilityManifest = BoundFactoryReproducibilityManifest
_factory.FactoryReproducibilityManifest = BoundFactoryReproducibilityManifest
_target.FactoryReproducibilityManifest = BoundFactoryReproducibilityManifest
_target.resolve_preregistered_binary_target_population = (
    _resolve_preregistered_binary_target_population
)
_install_factory_protocol_context()
