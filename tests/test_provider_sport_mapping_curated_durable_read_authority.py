from __future__ import annotations

from pathlib import Path

import pytest

import autosport.provider_sport_mapping as mapping


FUTURE = "2099-01-01T00:00:00Z"


def _registry(tmp_path: Path) -> mapping.ProviderSportMappingRegistry:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    registry.register_curated(
        provider_namespace="betfair",
        provider_sport_id="1",
    )
    return registry


def test_curated_resolution_rejects_path_read_text_rebind_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path)
    canonical_read_text = mapping.Path.read_text
    calls = 0

    def hostile_read_text(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return canonical_read_text(self, *args, **kwargs)

    monkeypatch.setattr(mapping.Path, "read_text", hostile_read_text)

    with pytest.raises(
        mapping.ProviderSportMappingError,
        match="durable|read|path|authority|dispatch",
    ):
        registry.resolve(
            provider_namespace="betfair",
            provider_sport_id="1",
            as_of=FUTURE,
        )

    assert calls == 0


def test_curated_publication_rejects_path_open_rebind_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    canonical_open = mapping.Path.open
    calls = 0

    def hostile_open(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return canonical_open(self, *args, **kwargs)

    monkeypatch.setattr(mapping.Path, "open", hostile_open)

    with pytest.raises(
        mapping.ProviderSportMappingError,
        match="durable|read|path|authority|dispatch",
    ):
        registry.register_curated(
            provider_namespace="betfair",
            provider_sport_id="1",
        )

    assert calls == 0
    assert registry.bindings == ()
