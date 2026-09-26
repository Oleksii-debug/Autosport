from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import autosport.provider_sport_mapping as mapping


def test_resolve_rejects_instance_rebound_durable_load(monkeypatch):
    with TemporaryDirectory() as tmp:
        registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
            Path(tmp) / "sport-map.json"
        )

        def forged_load() -> None:
            registry._bindings = [  # noqa: SLF001 - adversarial authority regression
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
            ]

        monkeypatch.setattr(registry, "_load", forged_load)

        with pytest.raises(mapping.ProviderSportMappingError, match="authority|canonical"):
            registry.resolve(
                provider_namespace="betfair",
                provider_sport_id="forged-provider-sport",
                as_of="2099-01-01T00:00:00Z",
            )
