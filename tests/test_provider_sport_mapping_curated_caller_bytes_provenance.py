from __future__ import annotations

import hashlib
import json

import pytest

from autosport.provider_sport_mapping import (
    ProviderSportMappingError,
    ProviderSportMappingRegistry,
)


def _caller_bytes(*, valid_from: str, valid_until: str | None) -> bytes:
    return json.dumps(
        {
            "schema": "autosport.provider_sport_mapping_evidence",
            "schema_version": 1,
            "provider_namespace": "betfair",
            "provider_sport_id": "1",
            "canonical_sport": "football",
            "valid_from": valid_from,
            "valid_until": valid_until,
            "evidence_available_at": valid_from,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_curated_identity_does_not_make_caller_validity_and_hash_positive_authority(tmp_path) -> None:
    registry = ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "provider-sport-mapping.json"
    )
    forged = _caller_bytes(
        valid_from="2026-01-01T00:00:00Z",
        valid_until=None,
    )
    forged_sha = hashlib.sha256(forged).hexdigest()

    with pytest.raises(
        ProviderSportMappingError,
        match="origin|curation|authority|provenance",
    ):
        registry.register_evidence(forged)

    assert registry.bindings == ()
    assert all(
        binding.source_snapshot_sha256 != forged_sha
        for binding in registry.bindings
    )


def test_caller_open_ended_curated_bytes_cannot_overlap_block_qualified_successor(tmp_path) -> None:
    registry = ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "provider-sport-mapping.json"
    )
    forged = _caller_bytes(
        valid_from="2026-01-01T00:00:00Z",
        valid_until=None,
    )

    with pytest.raises(
        ProviderSportMappingError,
        match="origin|curation|authority|provenance",
    ):
        registry.register_evidence(forged)

    # The caller must not be able to occupy the durable interval merely by choosing a
    # product-curated identity tuple. A later product-owned/fixed-origin issuer remains
    # free to publish its own qualified interval on this pristine registry.
    assert registry.bindings == ()
