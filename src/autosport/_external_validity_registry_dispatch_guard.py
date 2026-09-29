"""Seal external-validity provenance to product-issued evaluator truth.

The public registered-report builder composes two existing authorities only:
product-owned PolicyEvaluation issuance/re-resolution and the source-verified
ScientificRegistry reader.  It creates no evaluator, registry, or report authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import _external_validity_registry_core as _adapter
from . import _scientific_registry_read_authority as _read_authority
from . import external_validity_policy_issuance as _issuance
from . import scientific_registry as _registry


def _install_guard() -> None:
    adapter = _adapter
    registry_module = _registry
    read_authority_module = _read_authority
    issuance_module = _issuance

    registry_type = registry_module.ScientificRegistry
    adapter_error = adapter.ExternalValidityRegistryError
    read_authority_error = read_authority_module.ScientificRegistryReadAuthorityError
    read_authority_verifier = (
        read_authority_module.require_scientific_registry_read_authority
    )

    original_resolve = adapter._resolve_registered_origin
    original_bundle_hash = adapter.canonical_policy_evaluation_bundle_sha256
    original_text = adapter._text
    original_sha256 = adapter._sha256
    original_instant = adapter._instant
    original_report_builder = adapter.build_external_validity_report
    original_policy_type = adapter.PolicyEvaluation
    original_protocol_type = adapter.FrozenBaselineProtocol
    original_origin_type = adapter._RegisteredEvaluationOrigin
    original_mapping = adapter.Mapping

    original_workspace_type = adapter.ProductPolicyEvaluationWorkspace
    original_reference_type = adapter.IssuedPolicyEvaluationRef
    original_product_verify = adapter.verify_product_policy_evaluation
    original_product_error = adapter.ProductPolicyEvaluationIssuanceError
    original_core_verify_claim = adapter._verify_product_issued_claim

    def _verified_get_capability():
        if (
            registry_module.ScientificRegistry is not registry_type
            or adapter.ScientificRegistry is not registry_type
            or read_authority_module.require_scientific_registry_read_authority
            is not read_authority_verifier
        ):
            raise adapter_error(
                "ScientificRegistry read-authority verifier dispatch changed"
            )
        try:
            _read_fn, _validate_fn, get_fn, _causal_fn = read_authority_verifier()
        except read_authority_error as exc:
            raise adapter_error(
                "ScientificRegistry executable read authority was rebound"
            ) from exc
        return get_fn

    def _require_exact_registry_authority(registry: object):
        if type(registry) is not registry_type:
            raise adapter_error(
                "registry must be the exact ScientificRegistry authority"
            )
        instance_state = vars(registry)
        if set(instance_state) != {"path"}:
            raise adapter_error(
                "ScientificRegistry instance read authority was rebound"
            )
        _verified_get_capability()
        return registry

    def _registry_get(
        registry: object,
        record_type: str,
        record_id: str,
    ) -> Any:
        canonical_registry = _require_exact_registry_authority(registry)
        get_fn = _verified_get_capability()
        return get_fn(canonical_registry, record_type, record_id)

    def _assert_adapter_dispatch() -> None:
        if (
            adapter._require_exact_registry_authority
            is not _require_exact_registry_authority
            or adapter._registry_get is not _registry_get
            or adapter._resolve_registered_origin is not original_resolve
            or adapter.canonical_policy_evaluation_bundle_sha256
            is not original_bundle_hash
            or adapter._text is not original_text
            or adapter._sha256 is not original_sha256
            or adapter._instant is not original_instant
            or adapter.build_external_validity_report is not original_report_builder
            or adapter.PolicyEvaluation is not original_policy_type
            or adapter.FrozenBaselineProtocol is not original_protocol_type
            or adapter._RegisteredEvaluationOrigin is not original_origin_type
            or adapter.Mapping is not original_mapping
            or adapter.ProductPolicyEvaluationWorkspace is not original_workspace_type
            or adapter.IssuedPolicyEvaluationRef is not original_reference_type
            or adapter.ProductPolicyEvaluationIssuanceError is not original_product_error
            or adapter.verify_product_policy_evaluation is not original_product_verify
            or adapter._verify_product_issued_claim is not original_core_verify_claim
            or issuance_module.ProductPolicyEvaluationWorkspace
            is not original_workspace_type
            or issuance_module.IssuedPolicyEvaluationRef is not original_reference_type
            or issuance_module.ProductPolicyEvaluationIssuanceError
            is not original_product_error
            or issuance_module.verify_product_policy_evaluation
            is not original_product_verify
        ):
            raise adapter_error(
                "external-validity registry adapter dispatch changed"
            )

    def _verify_issued(
        authority: object,
        protocol: object,
        reference: object,
        claimed: object,
    ):
        try:
            return original_product_verify(
                authority,
                protocol,
                reference,
                claimed,
            )
        except original_product_error as exc:
            raise adapter_error(
                f"{getattr(claimed, 'policy_id', '<unknown>')}: "
                "evaluation is not exact product-issued evaluator truth"
            ) from exc

    def build_registered_external_validity_report(
        registry: object,
        protocol: object,
        candidate: object,
        baseline_results: Sequence[object],
        *,
        authority: object = None,
        candidate_issued_reference: object = None,
        baseline_issued_references: object = None,
        candidate_evaluation_bundle_id: str | None = None,
        baseline_evaluation_bundle_ids: object = None,
    ):
        """Build only after product-issued result re-resolution and registry proof."""

        if (
            adapter.build_registered_external_validity_report
            is not build_registered_external_validity_report
        ):
            raise adapter_error(
                "external-validity registry public dispatch changed"
            )
        _assert_adapter_dispatch()
        _verified_get_capability()

        canonical_registry = _require_exact_registry_authority(registry)
        if type(protocol) is not original_protocol_type:
            raise adapter_error(
                "protocol must be an exact FrozenBaselineProtocol value"
            )
        if type(candidate) is not original_policy_type:
            raise adapter_error(
                "candidate must be an exact PolicyEvaluation value"
            )
        if type(authority) is not original_workspace_type:
            raise adapter_error(
                "product-issued evaluation authority is required"
            )
        expected_registry_path = authority.workspace / "scientific_registry.json"
        if canonical_registry.path != expected_registry_path:
            raise adapter_error(
                "registry must be the canonical registry of the product evaluator workspace"
            )
        if type(candidate_issued_reference) is not original_reference_type:
            raise adapter_error(
                "candidate product-issued evaluation reference is required"
            )
        if candidate_issued_reference.baseline_kind is not None:
            raise adapter_error(
                "candidate issued reference must target the candidate protocol slot"
            )
        if not isinstance(baseline_issued_references, original_mapping):
            raise adapter_error(
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
            raise adapter_error(
                "baseline issued references must match supported frozen baselines"
                + (": " + "; ".join(detail) if detail else "")
            )

        by_id: dict[str, Any] = {}
        ordered_baseline_ids: list[str] = []
        for result in baseline_results:
            if type(result) is not original_policy_type:
                raise adapter_error(
                    "baseline_results must contain exact PolicyEvaluation values"
                )
            if result.policy_id in by_id:
                raise adapter_error(
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
            raise adapter_error(
                "baseline results must match supported frozen baselines"
                + (": " + "; ".join(detail) if detail else "")
            )

        verified_candidate = _verify_issued(
            authority,
            protocol,
            candidate_issued_reference,
            candidate,
        )
        candidate_bundle_id = candidate_issued_reference.evaluation_bundle_id
        if (
            candidate_evaluation_bundle_id is not None
            and candidate_evaluation_bundle_id != candidate_bundle_id
        ):
            raise adapter_error(
                "candidate bundle-id assertion does not match product-issued reference"
            )

        verified_by_id: dict[str, Any] = {}
        baseline_bundle_ids: dict[str, str] = {}
        for baseline_id in sorted(supported_ids):
            reference = baseline_issued_references[baseline_id]
            if type(reference) is not original_reference_type:
                raise adapter_error(
                    f"{baseline_id}: exact product-issued evaluation reference is required"
                )
            if reference.baseline_kind is None:
                raise adapter_error(
                    f"{baseline_id}: issued reference does not target a baseline protocol slot"
                )
            verified = _verify_issued(
                authority,
                protocol,
                reference,
                by_id[baseline_id],
            )
            if verified.policy_id != baseline_id:
                raise adapter_error(
                    f"{baseline_id}: product-issued evaluation policy identity mismatch"
                )
            verified_by_id[baseline_id] = verified
            baseline_bundle_ids[baseline_id] = reference.evaluation_bundle_id

        if baseline_evaluation_bundle_ids is not None:
            if not isinstance(baseline_evaluation_bundle_ids, original_mapping):
                raise adapter_error(
                    "baseline_evaluation_bundle_ids must be a mapping"
                )
            if set(baseline_evaluation_bundle_ids) != supported_ids:
                raise adapter_error(
                    "baseline bundle-id assertions must match supported frozen baselines"
                )
            for baseline_id in supported_ids:
                if (
                    baseline_evaluation_bundle_ids[baseline_id]
                    != baseline_bundle_ids[baseline_id]
                ):
                    raise adapter_error(
                        f"{baseline_id}: bundle-id assertion does not match product-issued reference"
                    )

        candidate_origin = original_resolve(
            canonical_registry,
            evaluation_bundle_id=candidate_bundle_id,
            evaluation=verified_candidate,
            protocol=protocol,
        )

        verified_baselines = tuple(
            verified_by_id[baseline_id] for baseline_id in ordered_baseline_ids
        )
        for baseline_id in sorted(supported_ids):
            origin = original_resolve(
                canonical_registry,
                evaluation_bundle_id=baseline_bundle_ids[baseline_id],
                evaluation=verified_by_id[baseline_id],
                protocol=protocol,
            )
            if origin.dataset_snapshot_id != candidate_origin.dataset_snapshot_id:
                raise adapter_error(
                    f"{baseline_id}: registry dataset snapshot differs from candidate"
                )
            if origin.protocol_sha256 != candidate_origin.protocol_sha256:
                raise adapter_error(
                    f"{baseline_id}: registry protocol SHA differs from candidate"
                )

        return original_report_builder(
            protocol,
            verified_candidate,
            verified_baselines,
        )

    build_registered_external_validity_report._autosport_external_validity_registry_dispatch_guard = True  # type: ignore[attr-defined]
    _require_exact_registry_authority._autosport_external_validity_registry_dispatch_guard = True  # type: ignore[attr-defined]
    _registry_get._autosport_external_validity_registry_dispatch_guard = True  # type: ignore[attr-defined]

    adapter._require_exact_registry_authority = _require_exact_registry_authority
    adapter._registry_get = _registry_get
    adapter.build_registered_external_validity_report = (
        build_registered_external_validity_report
    )


_install_guard()
del _install_guard
