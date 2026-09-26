from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import autosport.provider_sport_mapping as mapping


def test_register_evidence_rejects_rebound_strict_json_helper(monkeypatch):
    raw = b"not-json-provider-evidence"

    def forged_json(_raw_bytes: bytes) -> dict[str, object]:
        return {
            "schema": "autosport.provider_sport_mapping_evidence",
            "schema_version": 1,
            "provider_namespace": "betfair",
            "provider_sport_id": "forged-provider-sport",
            "canonical_sport": "football",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": None,
            "evidence_available_at": "2026-01-02T00:00:00Z",
        }

    monkeypatch.setattr(mapping, "_strict_json_object", forged_json)

    with TemporaryDirectory() as tmp:
        registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
            Path(tmp) / "sport-map.json"
        )
        with pytest.raises(mapping.ProviderSportMappingError, match="parser|authority"):
            registry.register_evidence(raw)
        assert registry.bindings == ()
