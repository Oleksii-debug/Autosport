from __future__ import annotations

import json
from pathlib import Path

import pytest

import autosport.provider_sport_mapping as mapping


def _evidence() -> bytes:
    return json.dumps(
        {
            "schema": "autosport.provider_sport_mapping_evidence",
            "schema_version": 1,
            "provider_namespace": "betfair",
            "provider_sport_id": "1",
            "canonical_sport": "football",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": None,
            "evidence_available_at": "2026-01-02T00:00:00Z",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_register_evidence_rejects_rebound_binding_constructor_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(tmp_path / "sport-map.json")
    canonical_binding_type = mapping.ProviderSportBinding
    calls = 0

    def forged_binding_constructor(**kwargs):
        nonlocal calls
        calls += 1
        return canonical_binding_type(**kwargs)

    monkeypatch.setattr(mapping, "ProviderSportBinding", forged_binding_constructor)

    with pytest.raises(
        mapping.ProviderSportMappingError,
        match="binding|constructor|canonical|authority|dispatch",
    ):
        registry.register_evidence(_evidence())

    assert calls == 0
    assert registry.bindings == ()
