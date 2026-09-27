from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import autosport.provider_sport_mapping as mapping


def _uncurated_bytes() -> bytes:
    return json.dumps(
        {
            "schema": "autosport.provider_sport_mapping_evidence",
            "schema_version": 1,
            "provider_namespace": "caller-provider",
            "provider_sport_id": "caller-sport",
            "canonical_sport": "football",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": None,
            "evidence_available_at": "2026-01-02T00:00:00Z",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fake_curated_payload() -> dict[str, object]:
    return {
        "schema": "autosport.provider_sport_mapping_evidence",
        "schema_version": 1,
        "provider_namespace": "betfair",
        "provider_sport_id": "1",
        "canonical_sport": "football",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_until": None,
        "evidence_available_at": "2026-01-02T00:00:00Z",
    }


def test_self_restoring_json_loads_cannot_split_curation_from_durable_registration() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        registry = mapping.ProviderSportMappingRegistry.initialize_pristine(path)
        canonical_loads = mapping.json.loads
        hostile_calls = 0

        def self_restoring_loads(*args, **kwargs):
            nonlocal hostile_calls
            del args, kwargs
            hostile_calls += 1
            mapping.json.loads = canonical_loads
            return _fake_curated_payload()

        mapping.json.loads = self_restoring_loads
        try:
            with pytest.raises(mapping.ProviderSportMappingError, match="parser|authority|dependency"):
                registry.register_evidence(_uncurated_bytes())
            # Product curation must reject the changed transitive parser graph before
            # executing it; in particular a first-pass fake must never be followed by
            # a different second-pass interpretation that reaches durable mutation.
            assert hostile_calls == 0
            assert registry.bindings == ()
            assert mapping.ProviderSportMappingRegistry(path).bindings == ()
        finally:
            mapping.json.loads = canonical_loads
