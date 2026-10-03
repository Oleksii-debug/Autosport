from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _registry(tmp_path: Path) -> mapping.ProviderSportMappingRegistry:
    return mapping.ProviderSportMappingRegistry.initialize_pristine(tmp_path / "sport-map.json")


def test_register_rejects_rebound_payload_before_hostile_writer(monkeypatch, tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    hostile_called = False

    def hostile_payload(self, bindings):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile _payload executed")

    monkeypatch.setattr(mapping.ProviderSportMappingRegistry, "_payload", hostile_payload)

    with pytest.raises(mapping.ProviderSportMappingError, match="writer dispatch authority"):
        registry.register_evidence(_curated_evidence())

    assert not hostile_called
    assert registry.bindings == ()


def test_register_rejects_rebound_unsigned_payload_before_hostile_writer(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    hostile_called = False

    def hostile_unsigned(bindings):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile _unsigned_payload executed")

    monkeypatch.setattr(
        mapping.ProviderSportMappingRegistry,
        "_unsigned_payload",
        staticmethod(hostile_unsigned),
    )

    with pytest.raises(mapping.ProviderSportMappingError, match="writer dispatch authority"):
        registry.register_evidence(_curated_evidence())

    assert not hostile_called
    assert registry.bindings == ()


def test_register_rejects_instance_payload_shadow_before_hostile_writer(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    hostile_called = False

    def hostile_payload(bindings):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile instance _payload executed")

    registry._payload = hostile_payload  # type: ignore[method-assign]

    with pytest.raises(mapping.ProviderSportMappingError, match="writer instance authority"):
        registry.register_evidence(_curated_evidence())

    assert not hostile_called
    assert registry.bindings == ()


def test_register_rejects_binding_payload_rebind_before_durable_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    hostile_called = False

    def hostile_payload(self):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile binding payload executed")

    monkeypatch.setattr(mapping.ProviderSportBinding, "payload", hostile_payload)

    with pytest.raises(mapping.ProviderSportMappingError, match="binding serialization authority"):
        registry.register_evidence(_curated_evidence())

    assert not hostile_called
    assert registry.bindings == ()


def test_register_rejects_atomic_writer_rebind_before_durable_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    hostile_called = False

    def hostile_writer(*args, **kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile atomic writer executed")

    monkeypatch.setattr(mapping, "atomic_write_json", hostile_writer)

    with pytest.raises(mapping.ProviderSportMappingError, match="writer dependency authority"):
        registry.register_evidence(_curated_evidence())

    assert not hostile_called
    assert registry.bindings == ()


def test_register_rejects_in_place_atomic_writer_code_swap_before_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)

    def hostile_writer(path, payload):
        del path, payload
        raise AssertionError("hostile atomic writer executable ran")

    monkeypatch.setattr(mapping.atomic_write_json, "__code__", hostile_writer.__code__)

    with pytest.raises(mapping.ProviderSportMappingError, match="writer dependency authority"):
        registry.register_evidence(_curated_evidence())

    assert registry.bindings == ()
