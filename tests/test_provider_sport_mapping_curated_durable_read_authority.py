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


def _install_concrete_override(path: Path, name: str, replacement):
    concrete_type = type(path)
    concrete_dict = type.__getattribute__(concrete_type, "__dict__")
    missing = object()
    previous = concrete_dict.get(name, missing)
    restored = False
    type.__setattr__(concrete_type, name, replacement)

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        if previous is missing:
            type.__delattr__(concrete_type, name)
        else:
            type.__setattr__(concrete_type, name, previous)

    return restore


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


def test_curated_publication_rejects_concrete_exists_override_before_execution(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    calls = 0
    restore = None

    def hostile_exists(self):
        nonlocal calls
        calls += 1
        assert restore is not None
        restore()
        return True

    restore = _install_concrete_override(registry.path, "exists", hostile_exists)
    try:
        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="durable|read|path|authority|dispatch",
        ):
            registry.register_curated(
                provider_namespace="betfair",
                provider_sport_id="1",
            )
    finally:
        restore()

    assert calls == 0
    assert registry.bindings == ()


def test_curated_resolution_rejects_concrete_read_text_override_before_execution(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    canonical_read_text = mapping.Path.read_text
    calls = 0
    restore = None

    def hostile_read_text(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        assert restore is not None
        restore()
        return canonical_read_text(self, *args, **kwargs)

    restore = _install_concrete_override(registry.path, "read_text", hostile_read_text)
    try:
        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="durable|read|path|authority|dispatch",
        ):
            registry.resolve(
                provider_namespace="betfair",
                provider_sport_id="1",
                as_of=FUTURE,
            )
    finally:
        restore()

    assert calls == 0


def test_curated_publication_rejects_concrete_open_override_before_execution(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    canonical_open = mapping.Path.open
    calls = 0
    restore = None

    def hostile_open(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        assert restore is not None
        restore()
        return canonical_open(self, *args, **kwargs)

    restore = _install_concrete_override(registry.path, "open", hostile_open)
    try:
        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="durable|read|path|authority|dispatch",
        ):
            registry.register_curated(
                provider_namespace="betfair",
                provider_sport_id="1",
            )
    finally:
        restore()

    assert calls == 0
    assert registry.bindings == ()
