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
    current = registry_type.register_evidence
    if getattr(current, "_product_curation_authority_guard", False):
        return

    evidence_descriptor = vars(mapping.ProviderSportEvidence).get("from_exact_bytes")
    if type(evidence_descriptor) is not classmethod:
        raise RuntimeError("ProviderSportEvidence.from_exact_bytes must remain a classmethod")
    canonical_parser = evidence_descriptor.__func__
    canonical_parser_code = canonical_parser.__code__
    canonical_registry_method = current
    canonical_registry_method_code = current.__code__
    canonical_evidence_type = mapping.ProviderSportEvidence
    canonical_error = mapping.ProviderSportMappingError
    curated = _PRODUCT_CURATED_PROVIDER_SPORTS

    def register_evidence(self, source_snapshot_bytes: bytes):
        # Fail before durable mutation if any authority-bearing surface moved.
        if registry_type.register_evidence is not register_evidence:
            raise canonical_error("provider sport curation authority changed")
        descriptor = vars(canonical_evidence_type).get("from_exact_bytes")
        if (
            descriptor is not evidence_descriptor
            or canonical_parser.__code__ is not canonical_parser_code
            or canonical_registry_method.__code__ is not canonical_registry_method_code
        ):
            raise canonical_error("provider sport curation authority changed")

        evidence = canonical_parser(canonical_evidence_type, source_snapshot_bytes)
        expected = curated.get((evidence.provider_namespace, evidence.provider_sport_id))
        if expected is None or evidence.canonical_sport != expected:
            raise canonical_error(
                "provider sport mapping lacks product-owned curation authority"
            )

        # Reuse the canonical registry implementation for every durability and
        # chronology invariant after product curation has admitted the identity.
        return canonical_registry_method(self, source_snapshot_bytes)

    setattr(register_evidence, "_product_curation_authority_guard", True)
    registry_type.register_evidence = register_evidence


_install_guard()
del _install_guard

__all__: list[str] = []
