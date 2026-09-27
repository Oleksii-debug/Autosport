from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

import autosport._provider_sport_mapping_curation_guard as curation_guard
import autosport.provider_sport_mapping as mapping


def _curated_evidence() -> bytes:
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


def test_register_ignores_guard_global_vars_forgery_before_durable_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A forged guard global cannot hide replacement of the canonical payload writer."""

    registry_type = mapping.ProviderSportMappingRegistry
    canonical_namespace = dict(builtins.vars(registry_type))
    registry = registry_type.initialize_pristine(tmp_path / "sport-map.json")
    hostile_called = False

    def hostile_payload(self, bindings):
        nonlocal hostile_called
        del self, bindings
        hostile_called = True
        raise AssertionError("hostile provider mapping payload writer reached")

    def forged_vars(value):
        if value is registry_type:
            return canonical_namespace
        return builtins.vars(value)

    monkeypatch.setattr(curation_guard, "vars", forged_vars, raising=False)
    monkeypatch.setattr(registry_type, "_payload", hostile_payload)

    with pytest.raises(
        mapping.ProviderSportMappingError,
        match="writer dispatch authority",
    ):
        registry.register_evidence(_curated_evidence())

    assert hostile_called is False
    assert registry.bindings == ()
