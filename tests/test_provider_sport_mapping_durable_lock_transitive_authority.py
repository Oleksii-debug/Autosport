from __future__ import annotations

from pathlib import Path

import pytest

import autosport.integrity as integrity
import autosport.provider_sport_mapping as mapping


def _public_lock_witness():
    public_lock = mapping.durable_path_lock
    assert public_lock.__closure__ is None
    return next(
        item
        for item in public_lock.__code__.co_consts
        if type(item) is tuple
        and len(item) >= 10
        and callable(item[0])
        and callable(item[5])
        and getattr(item[0], "__wrapped__", None) is item[5]
    )


def _guarded_lock_body():
    public_witness = _public_lock_witness()
    guarded_body = public_witness[5]
    assert guarded_body.__closure__ is None
    return guarded_body


def _inner_lock_witness():
    guarded_body = _guarded_lock_body()
    return next(
        item
        for item in guarded_body.__code__.co_consts
        if type(item) is tuple
        and len(item) >= 10
        and callable(item[0])
        and getattr(item[0], "__wrapped__", None) is item[2]
    )


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


def test_curated_publication_rejects_public_contextmanager_delegate_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    public_witness = _public_lock_witness()
    guarded_factory = public_witness[0]
    guarded_body = public_witness[5]
    guarded_closure = guarded_factory.__closure__
    assert guarded_closure is not None

    delegate_cell = next(
        cell
        for cell in guarded_closure
        if cell.cell_contents is guarded_body
    )
    hostile_calls = 0

    def hostile_guarded_body(_path):
        nonlocal hostile_calls
        hostile_calls += 1
        yield

    delegate_cell.cell_contents = hostile_guarded_body
    try:
        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="lock|durable|authority|delegation|wrapper",
        ):
            registry.register_curated(
                provider_namespace="betfair",
                provider_sport_id="1",
            )
    finally:
        delegate_cell.cell_contents = guarded_body

    assert hostile_calls == 0
    assert registry.bindings == ()


def test_curated_publication_rejects_inner_contextmanager_delegate_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )

    body_witness = _inner_lock_witness()
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


def test_curated_publication_rejects_contextmanager_runtime_class_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    public_witness = _public_lock_witness()
    contextmanager_globals = public_witness[2]
    canonical_contextmanager_type = public_witness[7]
    assert contextmanager_globals["_GeneratorContextManager"] is canonical_contextmanager_type
    hostile_calls = 0

    class HostileGeneratorContextManager:
        def __init__(self, *_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1

        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    contextmanager_globals["_GeneratorContextManager"] = HostileGeneratorContextManager
    try:
        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="contextmanager|lock|authority",
        ):
            registry.register_curated(
                provider_namespace="betfair",
                provider_sport_id="1",
            )
    finally:
        contextmanager_globals["_GeneratorContextManager"] = canonical_contextmanager_type

    assert hostile_calls == 0
    assert registry.bindings == ()


def test_curated_publication_rejects_contextmanager_enter_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )
    public_witness = _public_lock_witness()
    surface = public_witness[9]
    _name, enter_owner, canonical_enter, _enter_code = next(
        item for item in surface if item[0] == "__enter__"
    )
    hostile_calls = 0

    def hostile_enter(self):
        nonlocal hostile_calls
        hostile_calls += 1
        return self

    setattr(enter_owner, "__enter__", hostile_enter)
    try:
        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="contextmanager|lock|authority",
        ):
            registry.register_curated(
                provider_namespace="betfair",
                provider_sport_id="1",
            )
    finally:
        setattr(enter_owner, "__enter__", canonical_enter)

    assert hostile_calls == 0
    assert registry.bindings == ()


def test_curated_publication_still_uses_canonical_guarded_lock(
    tmp_path: Path,
) -> None:
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )

    binding = registry.register_curated(
        provider_namespace="betfair",
        provider_sport_id="1",
    )

    assert binding.provider_namespace == "betfair"
    assert binding.provider_sport_id == "1"
    assert binding.canonical_sport == "football"
    assert registry.bindings == (binding,)
