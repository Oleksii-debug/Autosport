from __future__ import annotations

"""Install product-owned curation before provider-sport mappings become positive truth.

Exact caller-authored JSON bytes prove integrity only.  They do not prove that a
provider sport identity maps to an Autosport canonical sport.  The generic durable
registry therefore remains the storage/chronology authority, while this thin guard
supplies the missing product curation authority for mappings that Autosport has
explicitly admitted.

The table is intentionally small and exact.  Adding another provider sport is a
source-reviewed product decision, not a runtime/caller capability.  The wrapped
registry still owns parsing, chronology, overlap checks, locking and persistence;
this module creates no second store or provider transport.
"""

from types import MappingProxyType

from . import provider_sport_mapping as mapping


# Betfair eventTypeId 1/2 are the product's currently admitted canonical mappings.
# Keep provider identifiers opaque; do not infer new mappings from textual shape.
_PRODUCT_CURATED_PROVIDER_SPORTS = MappingProxyType(
    {
        ("betfair", "1"): "football",
        ("betfair", "2"): "tennis",
    }
)


def _install_guard() -> None:
    registry_type = mapping.ProviderSportMappingRegistry
    current_register = registry_type.register_evidence
    current_resolve = registry_type.resolve
    if getattr(current_register, "_product_curation_authority_guard", False):
        if not getattr(current_resolve, "_product_curation_authority_guard", False):
            raise RuntimeError("provider sport curation guard is only partially installed")
        return

    evidence_descriptor = vars(mapping.ProviderSportEvidence).get("from_exact_bytes")
    if type(evidence_descriptor) is not classmethod:
        raise RuntimeError("ProviderSportEvidence.from_exact_bytes must remain a classmethod")
    canonical_parser = evidence_descriptor.__func__
    canonical_parser_code = canonical_parser.__code__
    canonical_registry_register = current_register
    canonical_registry_register_code = current_register.__code__
    canonical_registry_resolve = current_resolve
    canonical_registry_resolve_code = current_resolve.__code__
    canonical_evidence_type = mapping.ProviderSportEvidence
    canonical_resolution_type = mapping.CanonicalSportResolution
    canonical_error = mapping.ProviderSportMappingError
    curated = _PRODUCT_CURATED_PROVIDER_SPORTS

    def _require_curated(
        provider_namespace: str,
        provider_sport_id: str,
        canonical_sport: str,
    ) -> None:
        expected = curated.get((provider_namespace, provider_sport_id))
        if expected is None or canonical_sport != expected:
            raise canonical_error(
                "provider sport mapping lacks product-owned curation authority"
            )

    def _assert_guard_authority() -> None:
        if (
            registry_type.register_evidence is not register_evidence
            or registry_type.resolve is not resolve
        ):
            raise canonical_error("provider sport curation authority changed")
        descriptor = vars(canonical_evidence_type).get("from_exact_bytes")
        if (
            descriptor is not evidence_descriptor
            or canonical_parser.__code__ is not canonical_parser_code
            or canonical_registry_register.__code__ is not canonical_registry_register_code
            or canonical_registry_resolve.__code__ is not canonical_registry_resolve_code
        ):
            raise canonical_error("provider sport curation authority changed")

    def register_evidence(self, source_snapshot_bytes: bytes):
        # Fail before durable mutation if any authority-bearing surface moved.
        _assert_guard_authority()
        evidence = canonical_parser(canonical_evidence_type, source_snapshot_bytes)
        _require_curated(
            evidence.provider_namespace,
            evidence.provider_sport_id,
            evidence.canonical_sport,
        )

        # Reuse the canonical registry implementation for every durability and
        # chronology invariant after product curation has admitted the identity.
        return canonical_registry_register(self, source_snapshot_bytes)

    def resolve(self, *, provider_namespace: str, provider_sport_id: str, as_of: str):
        # A registry created by a pre-curation version can survive restart.  Durable
        # shape/digest truth is not sufficient to grandfather that old assertion into
        # positive product truth, so re-apply curation at every positive resolution.
        _assert_guard_authority()
        resolution = canonical_registry_resolve(
            self,
            provider_namespace=provider_namespace,
            provider_sport_id=provider_sport_id,
            as_of=as_of,
        )
        if type(resolution) is not canonical_resolution_type:
            raise canonical_error("provider sport resolution authority changed")
        _require_curated(
            resolution.provider_namespace,
            resolution.provider_sport_id,
            resolution.canonical_sport,
        )
        return resolution

    setattr(register_evidence, "_product_curation_authority_guard", True)
    setattr(resolve, "_product_curation_authority_guard", True)
    registry_type.register_evidence = register_evidence
    registry_type.resolve = resolve


_install_guard()
del _install_guard

__all__: list[str] = []
