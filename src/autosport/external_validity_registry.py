"""Durable scientific-registry provenance for external-validity comparisons.

The frozen baseline harness deliberately owns comparison semantics only.  This
adapter closes the provenance boundary by requiring every supported evaluation
to resolve to the existing immutable ScientificRegistry before a descriptive
external-validity report can be produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence

from .external_validity_baseline import (
    ExternalValidityError,
    ExternalValidityReport,
    FrozenBaselineProtocol,
    PolicyEvaluation,
    build_external_validity_report,
)
from .scientific_registry import ScientificRegistry


class ExternalValidityRegistryError(ExternalValidityError):
    """Raised when durable evaluation provenance is missing or inconsistent."""


@dataclass(frozen=True, slots=True)
class _RegisteredEvaluationOrigin:
    dataset_snapshot_id: str
    protocol_sha256: str


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ExternalValidityRegistryError(
            f"{field} must be a non-empty canonical string"
        )
    value.encode("utf-8")
    return value


def _sha256(value: object, field: str) -> str:
    text = _text(value, field).lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ExternalValidityRegistryError(f"{field} must be canonical SHA-256 hex")
    return text


def _instant(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExternalValidityRegistryError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExternalValidityRegistryError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _require_exact_registry_authority(registry: object) -> ScientificRegistry:
    """Return only the canonical durable registry implementation.

    Registry subclasses are deliberately rejected.  External-validity provenance
    is scientific evidence, so accepting an overrideable ``get`` method here would
    create a second, spoofable read authority beside the durable registry itself.
    """

    if type(registry) is not ScientificRegistry:
        raise ExternalValidityRegistryError(
            "registry must be the exact ScientificRegistry authority"
        )
    return registry


def _resolve_registered_origin(
    registry: ScientificRegistry,
    *,
    evaluation_bundle_id: str,
    evaluation: PolicyEvaluation,
    protocol: FrozenBaselineProtocol,
) -> _RegisteredEvaluationOrigin:
    canonical_registry = _require_exact_registry_authority(registry)
    bundle_id = _text(evaluation_bundle_id, "evaluation_bundle_id")
    bundle = ScientificRegistry.get(canonical_registry, "EvaluationBundle", bundle_id)
    if bundle is None:
        raise ExternalValidityRegistryError(
            f"{evaluation.policy_id}: registered EvaluationBundle is missing: {bundle_id}"
        )

    payload = bundle.payload
    registered_bundle_sha256 = _sha256(
        payload.get("bundle_sha256"), "EvaluationBundle.bundle_sha256"
    )
    if registered_bundle_sha256 != evaluation.evaluation_bundle_sha256:
        raise ExternalValidityRegistryError(
            f"{evaluation.policy_id}: evaluation bundle SHA does not match registry"
        )

    dataset_snapshot_id = _text(
        payload.get("dataset_snapshot_id"), "EvaluationBundle.dataset_snapshot_id"
    )
    registry_protocol_sha256 = _sha256(
        payload.get("protocol_sha256"), "EvaluationBundle.protocol_sha256"
    )

    dataset = ScientificRegistry.get(
        canonical_registry, "DatasetSnapshot", dataset_snapshot_id
    )
    if dataset is None:
        raise ExternalValidityRegistryError(
            f"{evaluation.policy_id}: EvaluationBundle references missing DatasetSnapshot: "
            f"{dataset_snapshot_id}"
        )

    dataset_payload = dataset.payload
    manifest_sha256 = _sha256(
        dataset_payload.get("manifest_sha256"), "DatasetSnapshot.manifest_sha256"
    )
    if manifest_sha256 != protocol.evidence_scope.dataset_sha256:
        raise ExternalValidityRegistryError(
            f"{evaluation.policy_id}: registry dataset manifest does not match frozen scope"
        )

    registry_cutoff = _instant(
        dataset_payload.get("causal_cutoff"), "DatasetSnapshot.causal_cutoff"
    )
    frozen_cutoff = _instant(
        protocol.evidence_scope.dataset_cutoff, "FrozenEvidenceScope.dataset_cutoff"
    )
    if registry_cutoff != frozen_cutoff:
        raise ExternalValidityRegistryError(
            f"{evaluation.policy_id}: registry dataset cutoff does not match frozen scope"
        )

    return _RegisteredEvaluationOrigin(
        dataset_snapshot_id=dataset_snapshot_id,
        protocol_sha256=registry_protocol_sha256,
    )


def build_registered_external_validity_report(
    registry: ScientificRegistry,
    protocol: FrozenBaselineProtocol,
    candidate: PolicyEvaluation,
    baseline_results: Sequence[PolicyEvaluation],
    *,
    candidate_evaluation_bundle_id: str,
    baseline_evaluation_bundle_ids: Mapping[str, str],
) -> ExternalValidityReport:
    """Build a frozen baseline report only from durable registered evaluations.

    `PolicyEvaluation` remains the comparison DTO.  This function supplies no
    new evaluation, ranking, significance, promotion, or execution authority;
    it only proves that each supported DTO refers to an already-durable bundle
    and that all compared bundles share one registered dataset/protocol origin.
    """

    canonical_registry = _require_exact_registry_authority(registry)
    if not isinstance(protocol, FrozenBaselineProtocol):
        raise ExternalValidityRegistryError("protocol must be FrozenBaselineProtocol")
    if not isinstance(candidate, PolicyEvaluation):
        raise ExternalValidityRegistryError("candidate must be PolicyEvaluation")
    if not isinstance(baseline_evaluation_bundle_ids, Mapping):
        raise ExternalValidityRegistryError(
            "baseline_evaluation_bundle_ids must be a mapping"
        )

    supported_ids = {
        definition.baseline_id
        for definition in protocol.baselines
        if definition.supported
    }
    supplied_bundle_ids = set(baseline_evaluation_bundle_ids)
    if supplied_bundle_ids != supported_ids:
        missing = sorted(supported_ids - supplied_bundle_ids)
        unexpected = sorted(supplied_bundle_ids - supported_ids)
        detail: list[str] = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unexpected:
            detail.append("unexpected=" + ",".join(unexpected))
        raise ExternalValidityRegistryError(
            "baseline evaluation bundle IDs must match supported frozen baselines"
            + (": " + "; ".join(detail) if detail else "")
        )

    by_id: dict[str, PolicyEvaluation] = {}
    for result in baseline_results:
        if not isinstance(result, PolicyEvaluation):
            raise ExternalValidityRegistryError(
                "baseline_results must contain PolicyEvaluation values"
            )
        if result.policy_id in by_id:
            raise ExternalValidityRegistryError(
                f"duplicate baseline result for policy_id: {result.policy_id}"
            )
        by_id[result.policy_id] = result

    candidate_origin = _resolve_registered_origin(
        canonical_registry,
        evaluation_bundle_id=candidate_evaluation_bundle_id,
        evaluation=candidate,
        protocol=protocol,
    )

    for baseline_id in sorted(supported_ids):
        result = by_id.get(baseline_id)
        if result is None:
            raise ExternalValidityRegistryError(
                f"supported baseline result is missing: {baseline_id}"
            )
        origin = _resolve_registered_origin(
            canonical_registry,
            evaluation_bundle_id=baseline_evaluation_bundle_ids[baseline_id],
            evaluation=result,
            protocol=protocol,
        )
        if origin.dataset_snapshot_id != candidate_origin.dataset_snapshot_id:
            raise ExternalValidityRegistryError(
                f"{baseline_id}: registry dataset snapshot differs from candidate"
            )
        if origin.protocol_sha256 != candidate_origin.protocol_sha256:
            raise ExternalValidityRegistryError(
                f"{baseline_id}: registry protocol SHA differs from candidate"
            )

    return build_external_validity_report(protocol, candidate, baseline_results)
