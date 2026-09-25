"""Seal external-validity provenance reads to canonical ScientificRegistry authority.

This guard owns no registry storage or comparison semantics.  It replaces only the
older adapter's cached registry-read seam with the already-integrated source-owned
ScientificRegistry read authority and fails closed if the adapter dispatch changes.
"""

from __future__ import annotations

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

    original_build = adapter.build_registered_external_validity_report
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

    def build_registered_external_validity_report(*args: Any, **kwargs: Any):
        if (
            adapter.build_registered_external_validity_report
            is not build_registered_external_validity_report
        ):
            raise adapter_error(
                "external-validity registry public dispatch changed"
            )
        _assert_adapter_dispatch()
        _verified_get_capability()
        return original_build(*args, **kwargs)

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
