from __future__ import annotations

import json

import pytest

from autosport.provider_sport_mapping import (
    ProviderSportMappingError,
    ProviderSportMappingRegistry,
)


def test_caller_authored_exact_bytes_cannot_mint_positive_mapping_resolution(tmp_path) -> None:
    """Exact-byte integrity is not provider-origin / curation authority.

    A caller can serialize a perfectly valid evidence-shaped JSON object and compute all
    public deterministic hashes. That must never be sufficient to make an arbitrary
    provider sport identity resolve as canonical product truth.

    Structural persistence is deliberately not the authority under test. An
    implementation may reject the uncurated blob before durable mutation, or it may
    retain non-authoritative structural evidence; either way positive resolution must
    remain impossible.
    """

    registry = ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "provider-sport-mapping.json"
    )
    caller_authored_bytes = json.dumps(
        {
            "schema": "autosport.provider_sport_mapping_evidence",
            "schema_version": 1,
            "provider_namespace": "caller-forged-provider",
            "provider_sport_id": "caller-forged-sport-id",
            "canonical_sport": "table_tennis",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": None,
            "evidence_available_at": "2026-01-01T00:00:00Z",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    try:
        registry.register_evidence(caller_authored_bytes)
    except ProviderSportMappingError:
        # Stronger fail-closed implementation: uncurated caller bytes never enter the
        # durable registry at all. The authority property is already satisfied.
        assert registry.bindings == ()
        return

    # If structural evidence is retained, it still must not become positive product
    # curation/origin authority at the resolution boundary.
    with pytest.raises(ProviderSportMappingError):
        registry.resolve(
            provider_namespace="caller-forged-provider",
            provider_sport_id="caller-forged-sport-id",
            as_of="2030-01-01T00:00:00Z",
        )
