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


def test_self_restoring_strict_json_helper_cannot_split_curation_from_registration() -> None:
    canonical = mapping._strict_json_object
    calls = 0

    def self_restoring_hostile(_raw_bytes: bytes) -> dict[str, object]:
        nonlocal calls
        calls += 1
        mapping._strict_json_object = canonical
        return _fake_curated_payload()

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        registry = mapping.ProviderSportMappingRegistry.initialize_pristine(path)
        mapping._strict_json_object = self_restoring_hostile
        try:
            with pytest.raises(mapping.ProviderSportMappingError, match="parser|authority"):
                registry.register_evidence(_uncurated_bytes())
            assert calls == 0
            assert registry.bindings == ()
            assert mapping.ProviderSportMappingRegistry(path).bindings == ()
        finally:
            mapping._strict_json_object = canonical


def test_forged_mapping_globals_stale_view_cannot_attest_hostile_real_parser() -> None:
    canonical = mapping._strict_json_object
    stale_namespace = dict(mapping.__dict__)
    calls = 0

    def self_restoring_hostile(_raw_bytes: bytes) -> dict[str, object]:
        nonlocal calls
        calls += 1
        mapping._strict_json_object = canonical
        return _fake_curated_payload()

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        registry = mapping.ProviderSportMappingRegistry.initialize_pristine(path)
        setattr(mapping, "globals", lambda: stale_namespace)
        mapping._strict_json_object = self_restoring_hostile
        try:
            with pytest.raises(mapping.ProviderSportMappingError, match="namespace|parser|authority"):
                registry.register_evidence(_uncurated_bytes())
            assert calls == 0
            assert registry.bindings == ()
            assert mapping.ProviderSportMappingRegistry(path).bindings == ()
        finally:
            mapping._strict_json_object = canonical
            delattr(mapping, "globals")
