"""Durable scientific-registry provenance for external-validity comparisons.

The frozen baseline harness deliberately owns comparison semantics only. This
adapter closes the provenance boundary by requiring every supported evaluation
to resolve to the existing immutable ScientificRegistry before a descriptive
external-validity report can be produced.
"""

from __future__ import annotations

import hashlib
import json
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
from .external_validity_policy_issuance import (
    IssuedPolicyEvaluationRef,
    ProductPolicyEvaluationIssuanceError,
    ProductPolicyEvaluationWorkspace,
    verify_product_policy_evaluation,
)
from .scientific_registry import ScientificRegistry


class ExternalValidityRegistryError(ExternalValidityError):
    """Raised when durable evaluation provenance is missing or inconsistent."""


@dataclass(frozen=True, slots=True)
class _RegisteredEvaluationOrigin:
    dataset_snapshot_id: str
    protocol_sha256: str


# Capture the concrete class read surface when this product adapter is imported.
# Positive provenance must never dispatch through caller-installed instance shadows
# or a later runtime class rebind. The registry remains the sole storage authority;
# these references only fence the executable read path used by this adapter.
_SCIENTIFIC_REGISTRY_GET = ScientificRegistry.get
_SCIENTIFIC_REGISTRY_READ = ScientificRegistry._read
_SCIENTIFIC_REGISTRY_VALIDATE_ENTRY = ScientificRegistry._validate_entry


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


def canonical_policy_evaluation_bundle_sha256(evaluation: PolicyEvaluation) -> str:
    """Commit an evaluation bundle SHA to the exact canonical result payload.

    The caller-carried ``evaluation_bundle_sha256`` is intentionally excluded from
    the commitment to avoid a recursive identity. Every scientific value that the
    descriptive comparison consumes is included, including baseline identity when
    present. Therefore an opaque matching SHA cannot bless changed result values.
    """

    if type(evaluation) is not PolicyEvaluation:
        raise ExternalValidityRegistryError(
            "evaluation must be an exact PolicyEvaluation value"
        )
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": "autosport_external_validity_policy_evaluation_bundle",
        "policy_id": evaluation.policy_id,
        "policy_artifact_sha256": evaluation.policy_artifact_sha256,
        "protocol_sha256": evaluation.protocol_sha256,
        "evidence_scope_sha256": evaluation.evidence_scope_sha256,
        "cohort_sha256": evaluation.cohort_sha256,
        "primary_metric": evaluation.primary_metric,
        "evaluated_at": evaluation.evaluated_at,
        "metric_value": evaluation.metric_value,
        "uncertainty_low": evaluation.uncertainty_low,
        "uncertainty_high": evaluation.uncertainty_high,
        "observed_count": evaluation.observed_count,
        "scored_count": evaluation.scored_count,
        "abstention_count": evaluation.abstention_count,
        "total_cost": evaluation.total_cost,
        "baseline_definition_sha256": evaluation.baseline_definition_sha256,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_exact_registry_authority(registry: object) -> ScientificRegistry:
    """Return only an unshadowed canonical durable registry implementation.

    Registry subclasses are deliberately rejected. Exact instances are also
    required to retain only their canonical ``path`` state: Python otherwise lets a
    caller shadow ``_read``/``get``/``_validate_entry`` on the instance and synthesize
    registry truth even when the class itself is canonical. Finally, the concrete
    class functions captured at adapter import must still be installed before any
    authority-bearing read occurs.
    """

    if type(registry) is not ScientificRegistry:
        raise ExternalValidityRegistryError(
            "registry must be the exact ScientificRegistry authority"
        )

    instance_state = vars(registry)
    if set(instance_state) != {"path"}:
        raise ExternalValidityRegistryError(
            "ScientificRegistry instance read authority was rebound"
        )
    if (
        ScientificRegistry.get is not _SCIENTIFIC_REGISTRY_GET
        or ScientificRegistry._read is not _SCIENTIFIC_REGISTRY_READ
        or ScientificRegistry._validate_entry is not _SCIENTIFIC_REGISTRY_VALIDATE_ENTRY
    ):
        raise ExternalValidityRegistryError(
            "ScientificRegistry executable read authority was rebound"
        )
    return registry


def _registry_get(
    registry: ScientificRegistry,
    record_type: str,
    record_id: str,
):
    """Read through the captured concrete capability after authority validation."""

    canonical_registry = _require_exact_registry_authority(registry)
    return _SCIENTIFIC_REGISTRY_GET(canonical_registry, record_type, record_id)


def _resolve_registered_origin(
    registry: ScientificRegistry,
    *,
    evaluation_bundle_id: str,
    evaluation: PolicyEvaluation,
    protocol: FrozenBaselineProtocol,
) -> _RegisteredEvaluationOrigin:
    canonical_registry = _require_exact_registry_authority(registry)
    if type(evaluation) is not PolicyEvaluation:
        raise ExternalValidityRegistryError(
            "evaluation must be an exact PolicyEvaluation value"
        )
    if type(protocol) is not FrozenBaselineProtocol:
        raise ExternalValidityRegistryError(
            "protocol must be an exact FrozenBaselineProtocol value"
        )

    bundle_id = _text(evaluation_bundle_id, "evaluation_bundle_id")
    bundle = _registry_get(canonical_registry, "EvaluationBundle", bundle_id)
    if bundle is None:
        raise ExternalValidityRegistryError(
            f"{evaluation.policy_id}: registered EvaluationBundle is missing: {bundle_id}"
        )

    payload = bundle.payload
    registered_bundle_sha256 = _sha256(
        payload.get("bundle_sha256"), "EvaluationBundle.bundle_sha256"
    )
    canonical_bundle_sha256 = canonical_policy_evaluation_bundle_sha256(evaluation)
    if evaluation.evaluation_bundle_sha256 != canonical_bundle_sha256:
        raise ExternalValidityRegistryError(
            f"{evaluation.policy_id}: evaluation bundle SHA does not commit to canonical evaluation payload"
        )
    if registered_bundle_sha256 != canonical_bundle_sha256:
        raise ExternalValidityRegistryError(
            f"{evaluation.policy_id}: registered bundle SHA does not match canonical evaluation commitment"
        )

    dataset_snapshot_id = _text(
        payload.get("dataset_snapshot_id"), "EvaluationBundle.dataset_snapshot_id"
    )
    registry_protocol_sha256 = _sha256(
        payload.get("protocol_sha256"), "EvaluationBundle.protocol_sha256"
    )
    if registry_protocol_sha256 != protocol.identity_sha256:
        raise ExternalValidityRegistryError(
            f"{evaluation.policy_id}: registered EvaluationBundle protocol SHA does not match frozen protocol"
        )

    dataset = _registry_get(
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


def _build_registered_external_validity_report_from_verified_evaluations(
    registry: ScientificRegistry,
    protocol: FrozenBaselineProtocol,
    candidate: PolicyEvaluation,
    baseline_results: Sequence[PolicyEvaluation],
    *,
    candidate_evaluation_bundle_id: str,
    baseline_evaluation_bundle_ids: Mapping[str, str],
) -> ExternalValidityReport:
    """Build a frozen baseline report only from durable registered evaluations.

    `PolicyEvaluation` remains the comparison DTO. This function supplies no
    new evaluation, ranking, significance, promotion, or execution authority;
    it only proves that each supported DTO refers to an already-durable bundle
    and that all compared bundles share one registered dataset/protocol origin.
    """

    canonical_registry = _require_exact_registry_authority(registry)
    if type(protocol) is not FrozenBaselineProtocol:
        raise ExternalValidityRegistryError(
            "protocol must be an exact FrozenBaselineProtocol value"
        )
    if type(candidate) is not PolicyEvaluation:
        raise ExternalValidityRegistryError(
            "candidate must be an exact PolicyEvaluation value"
        )
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
        if type(result) is not PolicyEvaluation:
            raise ExternalValidityRegistryError(
                "baseline_results must contain exact PolicyEvaluation values"
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


def _verify_product_issued_claim(
    authority: ProductPolicyEvaluationWorkspace,
    protocol: FrozenBaselineProtocol,
    reference: IssuedPolicyEvaluationRef,
    claimed: PolicyEvaluation,
) -> PolicyEvaluation:
    """Resolve one product-issued result and treat the caller DTO as assertion only."""

    try:
        return verify_product_policy_evaluation(
            authority,
            protocol,
            reference,
            claimed,
        )
    except ProductPolicyEvaluationIssuanceError as exc:
        raise ExternalValidityRegistryError(
            f"{getattr(claimed, 'policy_id', '<unknown>')}: "
            "evaluation is not exact product-issued evaluator truth"
        ) from exc


def build_registered_external_validity_report(
    registry: ScientificRegistry,
    protocol: FrozenBaselineProtocol,
    candidate: PolicyEvaluation,
    baseline_results: Sequence[PolicyEvaluation],
    *,
    authority: ProductPolicyEvaluationWorkspace | None = None,
    candidate_issued_reference: IssuedPolicyEvaluationRef | None = None,
    baseline_issued_references: Mapping[str, IssuedPolicyEvaluationRef] | None = None,
    candidate_evaluation_bundle_id: str | None = None,
    baseline_evaluation_bundle_ids: Mapping[str, str] | None = None,
) -> ExternalValidityReport:
    """Build only after exact product-issued evaluation re-resolution.

    Caller-carried PolicyEvaluation values are assertions, never issuance authority.
    The older explicit bundle-id arguments are optional assertions against bundle ids
    carried by the required issued references.
    """

    canonical_registry = _require_exact_registry_authority(registry)
    if type(protocol) is not FrozenBaselineProtocol:
        raise ExternalValidityRegistryError(
            "protocol must be an exact FrozenBaselineProtocol value"
        )
    if type(candidate) is not PolicyEvaluation:
        raise ExternalValidityRegistryError(
            "candidate must be an exact PolicyEvaluation value"
        )
    if type(authority) is not ProductPolicyEvaluationWorkspace:
        raise ExternalValidityRegistryError(
            "product-issued evaluation authority is required"
        )
    expected_registry_path = authority.workspace / "scientific_registry.json"
    if canonical_registry.path != expected_registry_path:
        raise ExternalValidityRegistryError(
            "registry must be the canonical registry of the product evaluator workspace"
        )
    if type(candidate_issued_reference) is not IssuedPolicyEvaluationRef:
        raise ExternalValidityRegistryError(
            "candidate product-issued evaluation reference is required"
        )
    if candidate_issued_reference.baseline_kind is not None:
        raise ExternalValidityRegistryError(
            "candidate issued reference must target the candidate protocol slot"
        )
    if not isinstance(baseline_issued_references, Mapping):
        raise ExternalValidityRegistryError(
            "baseline_issued_references must be a mapping"
        )

    supported_ids = {
        definition.baseline_id
        for definition in protocol.baselines
        if definition.supported
    }
    supplied_reference_ids = set(baseline_issued_references)
    if supplied_reference_ids != supported_ids:
        missing = sorted(supported_ids - supplied_reference_ids)
        unexpected = sorted(supplied_reference_ids - supported_ids)
        detail: list[str] = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unexpected:
            detail.append("unexpected=" + ",".join(unexpected))
        raise ExternalValidityRegistryError(
            "baseline issued references must match supported frozen baselines"
            + (": " + "; ".join(detail) if detail else "")
        )

    by_id: dict[str, PolicyEvaluation] = {}
    ordered_baseline_ids: list[str] = []
    for result in baseline_results:
        if type(result) is not PolicyEvaluation:
            raise ExternalValidityRegistryError(
                "baseline_results must contain exact PolicyEvaluation values"
            )
        if result.policy_id in by_id:
            raise ExternalValidityRegistryError(
                f"duplicate baseline result for policy_id: {result.policy_id}"
            )
        by_id[result.policy_id] = result
        ordered_baseline_ids.append(result.policy_id)
    if set(by_id) != supported_ids:
        missing = sorted(supported_ids - set(by_id))
        unexpected = sorted(set(by_id) - supported_ids)
        detail: list[str] = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unexpected:
            detail.append("unexpected=" + ",".join(unexpected))
        raise ExternalValidityRegistryError(
            "baseline results must match supported frozen baselines"
            + (": " + "; ".join(detail) if detail else "")
        )

    verified_candidate = _verify_product_issued_claim(
        authority,
        protocol,
        candidate_issued_reference,
        candidate,
    )
    effective_candidate_bundle_id = candidate_issued_reference.evaluation_bundle_id
    if (
        candidate_evaluation_bundle_id is not None
        and candidate_evaluation_bundle_id != effective_candidate_bundle_id
    ):
        raise ExternalValidityRegistryError(
            "candidate bundle-id assertion does not match product-issued reference"
        )

    verified_by_id: dict[str, PolicyEvaluation] = {}
    effective_baseline_bundle_ids: dict[str, str] = {}
    for baseline_id in sorted(supported_ids):
        reference = baseline_issued_references[baseline_id]
        if type(reference) is not IssuedPolicyEvaluationRef:
            raise ExternalValidityRegistryError(
                f"{baseline_id}: exact product-issued evaluation reference is required"
            )
        if reference.baseline_kind is None:
            raise ExternalValidityRegistryError(
                f"{baseline_id}: issued reference does not target a baseline protocol slot"
            )
        claimed = by_id[baseline_id]
        verified = _verify_product_issued_claim(
            authority,
            protocol,
            reference,
            claimed,
        )
        if verified.policy_id != baseline_id:
            raise ExternalValidityRegistryError(
                f"{baseline_id}: product-issued evaluation policy identity mismatch"
            )
        verified_by_id[baseline_id] = verified
        effective_baseline_bundle_ids[baseline_id] = reference.evaluation_bundle_id

    if baseline_evaluation_bundle_ids is not None:
        if not isinstance(baseline_evaluation_bundle_ids, Mapping):
            raise ExternalValidityRegistryError(
                "baseline_evaluation_bundle_ids must be a mapping"
            )
        if set(baseline_evaluation_bundle_ids) != supported_ids:
            raise ExternalValidityRegistryError(
                "baseline bundle-id assertions must match supported frozen baselines"
            )
        for baseline_id in supported_ids:
            if (
                baseline_evaluation_bundle_ids[baseline_id]
                != effective_baseline_bundle_ids[baseline_id]
            ):
                raise ExternalValidityRegistryError(
                    f"{baseline_id}: bundle-id assertion does not match product-issued reference"
                )

    verified_baselines = tuple(
        verified_by_id[baseline_id] for baseline_id in ordered_baseline_ids
    )
    return _build_registered_external_validity_report_from_verified_evaluations(
        canonical_registry,
        protocol,
        verified_candidate,
        verified_baselines,
        candidate_evaluation_bundle_id=effective_candidate_bundle_id,
        baseline_evaluation_bundle_ids=effective_baseline_bundle_ids,
    )
