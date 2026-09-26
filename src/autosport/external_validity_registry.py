"""Durable scientific-registry provenance for external-validity comparisons.

Public facade for the #758 adapter. The comparison implementation remains in one
private core module; import-time composition installs the canonical ScientificRegistry
read-authority guard before any public report builder is exported.
"""

from __future__ import annotations

from . import _external_validity_registry_core as _core
from . import _external_validity_registry_dispatch_guard as _dispatch_guard  # noqa: F401

ExternalValidityRegistryError = _core.ExternalValidityRegistryError
canonical_policy_evaluation_bundle_sha256 = (
    _core.canonical_policy_evaluation_bundle_sha256
)
build_registered_external_validity_report = (
    _core.build_registered_external_validity_report
)

__all__ = [
    "ExternalValidityRegistryError",
    "canonical_policy_evaluation_bundle_sha256",
    "build_registered_external_validity_report",
]
