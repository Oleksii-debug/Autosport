"""Seal external-validity provenance reads to canonical ScientificRegistry authority.

This guard owns no registry storage or comparison semantics. It replaces only the
older adapter's cached registry-read seam with the already-integrated source-owned
ScientificRegistry read authority and fails closed if the adapter dispatch changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from . import _external_validity_registry_core as _adapter
from . import _scientific_registry_read_authority as _read_authority
from . import scientific_registry as _registry


def _install_guard() -> None:
    adapter = _adapter
    registry_module = _registry
    read_authority_module = _read_authority

    registry_type = registry_module.ScientificRegistry
    adapter_error = adapter.ExternalValidityRegistryError
    authority_error = read_authority_module.ScientificRegistryReadAuthorityError
    authority_verifier = read_authority_module.require_scientific_registry_read_authority

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

    def _verified_get_capability():
        if (
            registry_module.ScientificRegistry is not registry_type
            or adapter.ScientificRegistry is not registry_type
            or read_authority_module.require_scientific_registry_read_authority
            is not authority_verifier
        ):
            raise adapter_error(
                "ScientificRegistry read-authority verifier dispatch changed"
            )
        try:
            _read_fn, _validate_fn, get_fn, _causal_fn = authority_verifier()
        except authority_error as exc:
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
        ):
            raise adapter_error(
                "external-validity registry adapter dispatch changed"
            )

    def build_registered_external_validity_report(
        registry: object,
        protocol: object,
        candidate: object,
        baseline_results: Sequence[object],
        *,
        candidate_evaluation_bundle_id: str,
        baseline_evaluation_bundle_ids: object,
    ):
        """Build the registered report without retaining predecessor build authority."""

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
        if not isinstance(baseline_evaluation_bundle_ids, original_mapping):
            raise adapter_error(
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
            raise adapter_error(
                "baseline evaluation bundle IDs must match supported frozen baselines"
                + (": " + "; ".join(detail) if detail else "")
            )

        by_id: dict[str, Any] = {}
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

        candidate_origin = original_resolve(
            canonical_registry,
            evaluation_bundle_id=candidate_evaluation_bundle_id,
            evaluation=candidate,
            protocol=protocol,
        )

        for baseline_id in sorted(supported_ids):
            result = by_id.get(baseline_id)
            if result is None:
                raise adapter_error(
                    f"supported baseline result is missing: {baseline_id}"
                )
            origin = original_resolve(
                canonical_registry,
                evaluation_bundle_id=baseline_evaluation_bundle_ids[baseline_id],
                evaluation=result,
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

        return original_report_builder(protocol, candidate, baseline_results)

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
