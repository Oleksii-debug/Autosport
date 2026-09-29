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


def test_curated_publication_rejects_inner_contextmanager_delegate_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )

    public_lock = mapping.durable_path_lock
    assert public_lock.__closure__ is None

    public_witness = next(
        item
        for item in public_lock.__code__.co_consts
        if type(item) is tuple
        and item
        and callable(item[0])
        and getattr(item[0], "__name__", "") == "guarded_durable_path_lock_body"
    )
    guarded_body = public_witness[0]
    assert guarded_body.__closure__ is None

    body_witness = next(
        item
        for item in guarded_body.__code__.co_consts
        if type(item) is tuple
        and item
        and callable(item[0])
        and getattr(item[0], "__wrapped__", None) is not None
    )
    inner_lock_factory = body_witness[0]
    canonical_inner_body = inner_lock_factory.__wrapped__
    inner_closure = inner_lock_factory.__closure__
    assert inner_closure is not None

    delegate_cell = next(
        cell
        for cell in inner_closure
        if cell.cell_contents is canonical_inner_body
    )
    hostile_calls = 0

    def hostile_lock_body(_path):
        nonlocal hostile_calls
        hostile_calls += 1
        yield

    delegate_cell.cell_contents = hostile_lock_body
    try:
        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="lock|durable|authority|delegation",
        ):
            registry.register_curated(
                provider_namespace="betfair",
                provider_sport_id="1",
            )
    finally:
        delegate_cell.cell_contents = canonical_inner_body

    assert hostile_calls == 0
    assert registry.bindings == ()
