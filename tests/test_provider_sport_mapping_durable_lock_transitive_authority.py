from __future__ import annotations

from pathlib import Path

import pytest

import autosport.integrity as integrity
import autosport.provider_sport_mapping as mapping


def test_curated_publication_rejects_transitive_lock_helper_retarget_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    canonical_thread_lock_for = integrity._thread_lock_for
    hostile_calls = 0

    def hostile_thread_lock_for(path):
        nonlocal hostile_calls
        hostile_calls += 1
        return canonical_thread_lock_for(path)

    monkeypatch.setattr(integrity, "_thread_lock_for", hostile_thread_lock_for)

    with pytest.raises(
        mapping.ProviderSportMappingError,
        match="lock|durable|authority|dependency",
    ):
        registry.register_curated(
            provider_namespace="betfair",
            provider_sport_id="1",
        )

    assert hostile_calls == 0
    assert registry.bindings == ()
