from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import autosport.provider_sport_mapping as mapping


def _publish_curated(registry: mapping.ProviderSportMappingRegistry) -> None:
    registry.register_curated(
        provider_namespace="betfair",
        provider_sport_id="1",
    )


def test_resolve_rejects_rebound_persisted_binding_decoder(monkeypatch):
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        registry = mapping.ProviderSportMappingRegistry.initialize_pristine(path)
        _publish_curated(registry)

        original_descriptor = vars(mapping.ProviderSportBinding)["from_payload"]

        def forged_from_payload(cls, payload):
            del cls, payload
            return mapping.ProviderSportBinding(
                provider_namespace="betfair",
                provider_sport_id="forged",
                canonical_sport="football",
                valid_from="2026-01-01T00:00:00Z",
                valid_until=None,
                evidence_available_at="2026-01-02T00:00:00Z",
                source_snapshot_sha256="a" * 64,
                recorded_at="2026-01-02T00:00:01Z",
            )

        monkeypatch.setattr(
            mapping.ProviderSportBinding,
            "from_payload",
            classmethod(forged_from_payload),
        )
        assert vars(mapping.ProviderSportBinding)["from_payload"] is not original_descriptor

        with pytest.raises(mapping.ProviderSportMappingError, match="authority|canonical|decoder"):
            registry.resolve(
                provider_namespace="betfair",
                provider_sport_id="forged",
                as_of="2099-01-01T00:00:00Z",
            )


def test_resolve_rejects_in_place_registry_load_code_swap(monkeypatch):
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        registry = mapping.ProviderSportMappingRegistry.initialize_pristine(path)
        _publish_curated(registry)
        canonical_load = mapping.ProviderSportMappingRegistry._load

        def forged_load(self) -> None:
            self._bindings = [
                mapping.ProviderSportBinding(
                    provider_namespace="betfair",
                    provider_sport_id="forged",
                    canonical_sport="football",
                    valid_from="2026-01-01T00:00:00Z",
                    valid_until=None,
                    evidence_available_at="2026-01-02T00:00:00Z",
                    source_snapshot_sha256="b" * 64,
                    recorded_at="2026-01-02T00:00:01Z",
                )
            ]

        monkeypatch.setattr(canonical_load, "__code__", forged_load.__code__)

        with pytest.raises(mapping.ProviderSportMappingError, match="authority|canonical|executable"):
            registry.resolve(
                provider_namespace="betfair",
                provider_sport_id="forged",
                as_of="2099-01-01T00:00:00Z",
            )
