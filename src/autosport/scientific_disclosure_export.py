"""Product-owned scientific outcome export with consume-before-publish semantics.

``ScientificRegistry.reproducibility_bundle`` contains the durable Experiment outcome.
That makes its file-export path an adaptive disclosure boundary, even though the
bundle otherwise contains references and hashes.  This module binds that outward
projection to the exact durable Experiment/EvaluationBundle lineage, derives the
physical holdout identity from canonical registry truth, consumes it through the
existing HoldoutDisclosureGate, and only then publishes the JSON file.

PromotionEvidence/PromotionDecision are additional lineage when present, not a
precondition for disclosing a negative, null, harmful, inconclusive, or otherwise
non-promoted Experiment.  Requiring promotion would make the safety boundary
silently suppress the negative-result retention required by the scientific policy.

No second holdout store, scientific registry, or promotion authority is introduced.
The legacy direct ``ScientificRegistry.export_reproducibility_bundle`` entrypoint is
made fail-closed when this product module is installed so callers cannot bypass the
consume-before-publish boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .holdout_disclosure import (
    DisclosureChannel,
    DisclosureKind,
    HoldoutDisclosureGate,
)
from .integrity import atomic_write_json
from .point_in_time_evidence import HoldoutConsumptionLedger
from .scientific_registry import (
    DatasetSnapshot,
    ScientificRegistry,
    promotion_holdout_access_id,
)


_MAX_AS_OF = "9999-12-31T23:59:59.999999+00:00"
_DIRECT_EXPORT_BLOCKED = (
    "direct scientific reproducibility export is disabled; use "
    "ScientificDisclosureExporter so holdout consumption is bound before publication"
)

_CANONICAL_GET = ScientificRegistry.get
_CANONICAL_CAUSAL_RECORDS = ScientificRegistry.causal_records
_CANONICAL_REPRODUCIBILITY_BUNDLE = ScientificRegistry.reproducibility_bundle
_ORIGINAL_DIRECT_EXPORT = ScientificRegistry.export_reproducibility_bundle


class ScientificDisclosureExportError(RuntimeError):
    """The outward scientific result could not be bound to canonical holdout truth."""


@dataclass(frozen=True, slots=True)
class ScientificDisclosureExportResult:
    bundle_sha256: str
    experiment_id: str
    promotion_evidence_id: str | None
    promotion_decision_id: str | None
    holdout_consumption_id: str


def _blocked_direct_export(
    self: ScientificRegistry,
    experiment_id: str,
    path: str | Path,
) -> str:
    del self, experiment_id, path
    raise ScientificDisclosureExportError(_DIRECT_EXPORT_BLOCKED)


def _install_direct_export_fence() -> None:
    current = ScientificRegistry.export_reproducibility_bundle
    if current is _blocked_direct_export:
        return
    if current is not _ORIGINAL_DIRECT_EXPORT:
        raise RuntimeError(
            "ScientificRegistry.export_reproducibility_bundle changed before disclosure fence installation"
        )
    ScientificRegistry.export_reproducibility_bundle = _blocked_direct_export


def _require_registry_surface(registry: ScientificRegistry) -> None:
    if type(registry) is not ScientificRegistry:
        raise ScientificDisclosureExportError(
            "canonical disclosure registry must be an exact ScientificRegistry"
        )
    for name in ("get", "causal_records", "reproducibility_bundle"):
        if name in vars(registry):
            raise ScientificDisclosureExportError(
                f"canonical ScientificRegistry dispatch must not be instance-shadowed: {name}"
            )
    if ScientificRegistry.get is not _CANONICAL_GET:
        raise ScientificDisclosureExportError("ScientificRegistry.get dispatch changed")
    if ScientificRegistry.causal_records is not _CANONICAL_CAUSAL_RECORDS:
        raise ScientificDisclosureExportError("ScientificRegistry.causal_records dispatch changed")
    if ScientificRegistry.reproducibility_bundle is not _CANONICAL_REPRODUCIBILITY_BUNDLE:
        raise ScientificDisclosureExportError(
            "ScientificRegistry.reproducibility_bundle dispatch changed"
        )
    if ScientificRegistry.export_reproducibility_bundle is not _blocked_direct_export:
        raise ScientificDisclosureExportError(
            "direct scientific export fence is not installed"
        )


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if type(value) is not str or not value or value != value.strip():
        raise ScientificDisclosureExportError(
            f"canonical scientific lineage lacks {key}"
        )
    return value


def _entry(registry: ScientificRegistry, record_type: str, record_id: str):
    value = _CANONICAL_GET(registry, record_type, record_id)
    if value is None:
        raise ScientificDisclosureExportError(
            f"canonical scientific lineage is missing {record_type}:{record_id}"
        )
    return value


def _optional_single(values: list, name: str):
    if len(values) > 1:
        raise ScientificDisclosureExportError(
            f"scientific disclosure has ambiguous canonical {name}; found {len(values)}"
        )
    return values[0] if values else None


def _confirmation_trial_family_id(research_protocol_id: str) -> str:
    """Return the product-owned confirmation family used by registry promotion law."""

    if type(research_protocol_id) is not str or not research_protocol_id:
        raise ScientificDisclosureExportError(
            "research_protocol_id must be canonical before deriving trial family"
        )
    return f"{research_protocol_id}:confirmation-trial-family"


class ScientificDisclosureExporter:
    """Publish one outcome-bearing reproducibility bundle after durable consumption.

    The caller supplies no dataset, protocol, trial-family, evidence, or decision
    identity.  Dataset/protocol/evaluation truth is re-resolved from the exact
    ScientificRegistry bound to the gate's canonical DatasetSnapshotLineageAuthority.
    The confirmation family is the same deterministic product convention enforced
    by ScientificRegistry promotion validation.  Promotion records, when present,
    are verified as additional lineage but cannot become an export prerequisite.
    """

    def __init__(self, gate: HoldoutDisclosureGate) -> None:
        if type(gate) is not HoldoutDisclosureGate:
            raise ScientificDisclosureExportError(
                "gate must be the exact canonical HoldoutDisclosureGate"
            )
        ledger = getattr(gate, "_ledger", None)
        if type(ledger) is not HoldoutConsumptionLedger:
            raise ScientificDisclosureExportError(
                "disclosure gate is not bound to the canonical HoldoutConsumptionLedger"
            )
        lineage = getattr(ledger, "_dataset_lineage_authority", None)
        registry = getattr(lineage, "registry", None)
        if type(registry) is not ScientificRegistry:
            raise ScientificDisclosureExportError(
                "disclosure gate lacks canonical dataset-lineage ScientificRegistry authority"
            )
        _require_registry_surface(registry)
        self._gate = gate
        self._ledger = ledger
        self._lineage = lineage
        self._registry = registry

    def _require_stable_authority(self) -> None:
        if getattr(self._gate, "_ledger", None) is not self._ledger:
            raise ScientificDisclosureExportError(
                "disclosure gate ledger authority changed after exporter construction"
            )
        if getattr(self._ledger, "_dataset_lineage_authority", None) is not self._lineage:
            raise ScientificDisclosureExportError(
                "holdout dataset lineage authority changed after exporter construction"
            )
        if getattr(self._lineage, "registry", None) is not self._registry:
            raise ScientificDisclosureExportError(
                "scientific registry authority changed after exporter construction"
            )
        _require_registry_surface(self._registry)

    def _resolve(self, experiment_id: str):
        self._require_stable_authority()
        if type(experiment_id) is not str or not experiment_id or experiment_id != experiment_id.strip():
            raise ScientificDisclosureExportError(
                "experiment_id must be a non-empty canonical string"
            )

        experiment = _entry(self._registry, "Experiment", experiment_id)
        ep = experiment.payload
        protocol_id = _text(ep, "research_protocol_id")
        dataset_id = _text(ep, "dataset_snapshot_id")
        evaluation_id = _text(ep, "evaluation_bundle_id")
        strategy_id = _text(ep, "strategy_version_id")
        model_id = ep.get("model_version_id")
        if model_id is not None and (type(model_id) is not str or not model_id):
            raise ScientificDisclosureExportError(
                "canonical experiment has invalid model_version_id"
            )

        protocol = _entry(self._registry, "ResearchProtocol", protocol_id)
        dataset = _entry(self._registry, "DatasetSnapshot", dataset_id)
        evaluation = _entry(self._registry, "EvaluationBundle", evaluation_id)

        if evaluation.payload.get("dataset_snapshot_id") != dataset_id:
            raise ScientificDisclosureExportError(
                "EvaluationBundle dataset does not match disclosed Experiment"
            )
        protocol_sha = _text(protocol.payload, "protocol_sha256")
        if evaluation.payload.get("protocol_sha256") != protocol_sha:
            raise ScientificDisclosureExportError(
                "EvaluationBundle protocol does not match canonical ResearchProtocol"
            )
        if protocol.payload.get("dataset_manifest_sha256") != dataset.payload.get(
            "manifest_sha256"
        ):
            raise ScientificDisclosureExportError(
                "ResearchProtocol dataset manifest does not match disclosed DatasetSnapshot"
            )
        if evaluation.payload.get("evaluated_strategy_version_id") != strategy_id:
            raise ScientificDisclosureExportError(
                "EvaluationBundle strategy does not match disclosed Experiment"
            )
        if evaluation.payload.get("evaluated_model_version_id") != model_id:
            raise ScientificDisclosureExportError(
                "EvaluationBundle model does not match disclosed Experiment"
            )

        evaluation_sha = _text(evaluation.payload, "bundle_sha256")
        trial_family_id = _confirmation_trial_family_id(protocol_id)
        expected_holdout_id = promotion_holdout_access_id(
            research_protocol_id=protocol_id,
            dataset_manifest_sha256=_text(dataset.payload, "manifest_sha256"),
            source_identity=_text(dataset.payload, "source_identity"),
            license_identity=_text(dataset.payload, "license_identity"),
            confirmation_trial_family_id=trial_family_id,
        )

        # Promotion is not required to publish a scientific result.  When typed
        # PromotionEvidence exists for this exact Experiment, it must agree with
        # the same physical confirmation family/holdout identity used by export.
        evidence_candidates = [
            entry
            for entry in _CANONICAL_CAUSAL_RECORDS(
                self._registry,
                "PromotionEvidence",
                as_of=_MAX_AS_OF,
            )
            if entry.payload.get("experiment_id") == experiment_id
            and entry.payload.get("research_protocol_id") == protocol_id
            and entry.payload.get("dataset_snapshot_id") == dataset_id
            and entry.payload.get("evaluation_bundle_id") == evaluation_id
            and entry.payload.get("candidate_strategy_version_id") == strategy_id
            and entry.payload.get("candidate_model_version_id") == model_id
        ]
        evidence = _optional_single(evidence_candidates, "PromotionEvidence")
        decision = None
        if evidence is not None:
            ev = evidence.payload
            if ev.get("evaluation_bundle_sha256") != evaluation_sha:
                raise ScientificDisclosureExportError(
                    "PromotionEvidence bundle digest does not match disclosed EvaluationBundle"
                )
            if ev.get("confirmation_trial_family_id") != trial_family_id:
                raise ScientificDisclosureExportError(
                    "PromotionEvidence trial family does not match canonical protocol family"
                )
            if ev.get("holdout_access_id") != expected_holdout_id:
                raise ScientificDisclosureExportError(
                    "PromotionEvidence holdout identity does not match canonical physical lineage"
                )

            decision_candidates = [
                entry
                for entry in _CANONICAL_CAUSAL_RECORDS(
                    self._registry,
                    "PromotionDecision",
                    as_of=_MAX_AS_OF,
                )
                if entry.payload.get("promotion_evidence_id") == evidence.record_id
                and entry.payload.get("research_protocol_id") == protocol_id
                and entry.payload.get("evaluation_bundle_id") == evaluation_id
                and entry.payload.get("candidate_strategy_version_id") == strategy_id
                and entry.payload.get("candidate_model_version_id") == model_id
            ]
            decision = _optional_single(decision_candidates, "PromotionDecision")
            if decision is not None:
                if decision.payload.get("evaluation_bundle_sha256") != evaluation_sha:
                    raise ScientificDisclosureExportError(
                        "PromotionDecision bundle digest does not match disclosed EvaluationBundle"
                    )
                if decision.payload.get("protocol_sha256") != protocol_sha:
                    raise ScientificDisclosureExportError(
                        "PromotionDecision protocol digest does not match disclosed ResearchProtocol"
                    )

        canonical_snapshot = DatasetSnapshot(
            dataset_snapshot_id=dataset.record_id,
            manifest_sha256=_text(dataset.payload, "manifest_sha256"),
            source_identity=_text(dataset.payload, "source_identity"),
            license_identity=_text(dataset.payload, "license_identity"),
            causal_cutoff=_text(dataset.payload, "causal_cutoff"),
            available_at_utc=dataset.available_at,
            outcome_reveal_after=dataset.payload.get("outcome_reveal_after"),
        )
        return experiment, evidence, decision, canonical_snapshot, protocol_id, trial_family_id

    def export_reproducibility_bundle(
        self,
        experiment_id: str,
        path: str | Path,
        *,
        disclosed_at_utc: str,
    ) -> ScientificDisclosureExportResult:
        (
            experiment,
            evidence,
            decision,
            snapshot,
            protocol_id,
            trial_family_id,
        ) = self._resolve(experiment_id)

        disclosure = self._gate.record(
            dataset_snapshot=snapshot,
            research_protocol_id=protocol_id,
            confirmation_trial_family_id=trial_family_id,
            channel=DisclosureChannel.EXPORT,
            kind=DisclosureKind.EVENT_OUTCOME,
            accessible_to_adaptive_actor=True,
            disclosed_at_utc=disclosed_at_utc,
        )

        # Consumption is durable before any outward payload is materialized.  A
        # publication failure therefore conservatively burns the same physical
        # confirmation capacity; exact retry converges on the same ledger record.
        bundle = _CANONICAL_REPRODUCIBILITY_BUNDLE(
            self._registry,
            experiment.record_id,
        )
        exported_experiment = bundle.get("experiment")
        if type(exported_experiment) is not dict or exported_experiment.get(
            "outcome"
        ) != experiment.payload.get("outcome"):
            raise ScientificDisclosureExportError(
                "reproducibility export outcome is not bound to canonical Experiment"
            )
        atomic_write_json(path, bundle)
        bundle_sha = bundle.get("bundle_sha256")
        if type(bundle_sha) is not str or not bundle_sha:
            raise ScientificDisclosureExportError(
                "reproducibility bundle lacks canonical bundle_sha256"
            )
        return ScientificDisclosureExportResult(
            bundle_sha256=bundle_sha,
            experiment_id=experiment.record_id,
            promotion_evidence_id=(None if evidence is None else evidence.record_id),
            promotion_decision_id=(None if decision is None else decision.record_id),
            holdout_consumption_id=disclosure.consumption.consumption_id,
        )


_install_direct_export_fence()


__all__ = [
    "ScientificDisclosureExportError",
    "ScientificDisclosureExportResult",
    "ScientificDisclosureExporter",
]
