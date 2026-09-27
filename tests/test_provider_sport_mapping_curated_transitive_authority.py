from __future__ import annotations

from pathlib import Path

import pytest

import autosport.provider_sport_mapping as mapping


def _registry(tmp_path: Path) -> mapping.ProviderSportMappingRegistry:
    return mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )


def _publish(registry: mapping.ProviderSportMappingRegistry):
    return registry.register_curated(
        provider_namespace="betfair",
        provider_sport_id="1",
    )


def test_curated_publication_rejects_sha256_dependency_rebind_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path)
    canonical_sha256 = mapping.hashlib.sha256
    calls = 0

    def hostile_sha256(*args, **kwargs):
        nonlocal calls
        calls += 1
        return canonical_sha256(*args, **kwargs)

    monkeypatch.setattr(mapping.hashlib, "sha256", hostile_sha256)

    with pytest.raises(
        mapping.ProviderSportMappingError,
        match="hash|digest|dependency|authority",
    ):
        _publish(registry)

    assert calls == 0
    assert registry.bindings == ()


def test_curated_publication_rejects_json_dumps_rebind_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path)
    canonical_dumps = mapping.json.dumps
    calls = 0

    def hostile_dumps(*args, **kwargs):
        nonlocal calls
        calls += 1
        return canonical_dumps(*args, **kwargs)

    monkeypatch.setattr(mapping.json, "dumps", hostile_dumps)

    with pytest.raises(
        mapping.ProviderSportMappingError,
        match="json|digest|dependency|authority",
    ):
        _publish(registry)

    assert calls == 0
    assert registry.bindings == ()


def test_curated_publication_rejects_evidence_field_dispatch_rebind_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path)
    canonical_getattribute = mapping.ProviderSportEvidence.__getattribute__
    calls = 0

    def hostile_getattribute(self, name):
        nonlocal calls
        calls += 1
        return canonical_getattribute(self, name)

    monkeypatch.setattr(
        mapping.ProviderSportEvidence,
        "__getattribute__",
        hostile_getattribute,
    )

    with pytest.raises(
        mapping.ProviderSportMappingError,
        match="evidence|field|surface|authority|dispatch",
    ):
        _publish(registry)

    assert calls == 0
    assert registry.bindings == ()


def test_curated_publication_rejects_binding_field_dispatch_rebind_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(tmp_path)
    canonical_getattribute = mapping.ProviderSportBinding.__getattribute__
    calls = 0

    def hostile_getattribute(self, name):
        nonlocal calls
        calls += 1
        return canonical_getattribute(self, name)

    monkeypatch.setattr(
        mapping.ProviderSportBinding,
        "__getattribute__",
        hostile_getattribute,
    )

    with pytest.raises(
        mapping.ProviderSportMappingError,
        match="binding|field|surface|authority|dispatch",
    ):
        _publish(registry)

    assert calls == 0
    assert registry.bindings == ()
