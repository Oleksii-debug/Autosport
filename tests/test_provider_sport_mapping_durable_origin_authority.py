from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import autosport.provider_sport_mapping as mapping


def test_resolve_never_uses_caller_populated_memory_without_durable_registry():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "missing-sport-map.json"
        registry = mapping.ProviderSportMappingRegistry(path)
        registry._bindings.append(  # noqa: SLF001 - adversarial authority regression
            mapping.ProviderSportBinding(
                provider_namespace="betfair",
                provider_sport_id="forged-provider-sport",
                canonical_sport="football",
                valid_from="2026-01-01T00:00:00Z",
                valid_until=None,
                evidence_available_at="2026-01-02T00:00:00Z",
                source_snapshot_sha256="a" * 64,
                recorded_at="2026-01-02T00:00:01Z",
            )
        )

        assert not path.exists()
        with pytest.raises(mapping.ProviderSportMappingError, match="registry|durable|missing"):
            registry.resolve(
                provider_namespace="betfair",
                provider_sport_id="forged-provider-sport",
                as_of="2099-01-01T00:00:00Z",
            )
        assert not path.exists()


def test_register_curated_requires_initialized_durable_registry():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "missing-sport-map.json"
        registry = mapping.ProviderSportMappingRegistry(path)

        with pytest.raises(mapping.ProviderSportMappingError, match="registry|durable|initialize"):
            registry.register_curated(
                provider_namespace="betfair",
                provider_sport_id="1",
            )
        assert registry.bindings == ()
        assert not path.exists()


def test_register_evidence_rejects_caller_bytes_even_when_registry_is_missing():
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "missing-sport-map.json"
        registry = mapping.ProviderSportMappingRegistry(path)
        raw = (
            b'{"canonical_sport":"football","evidence_available_at":"2026-09-27T12:10:00Z",'
            b'"provider_namespace":"betfair","provider_sport_id":"1",'
            b'"schema":"autosport.provider_sport_mapping_evidence","schema_version":1,'
            b'"valid_from":"2026-09-27T12:10:00Z","valid_until":null}'
        )

        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="caller-authored|positive|authority|register_curated",
        ):
            registry.register_evidence(raw)
        assert registry.bindings == ()
        assert not path.exists()
