from __future__ import annotations

from pathlib import Path

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


def _public_witness():
    public_lock = mapping.durable_path_lock
    assert public_lock.__closure__ is None
    return next(
        item
        for item in public_lock.__code__.co_consts
        if type(item) is tuple
        and len(item) >= 16
        and callable(item[0])
        and callable(item[5])
        and getattr(item[0], "__wrapped__", None) is item[5]
    )


def _assert_publication_fails_closed(
    registry: mapping.ProviderSportMappingRegistry,
    expected_message: str,
) -> None:
    try:
        _publish(registry)
    except mapping.ProviderSportMappingError as exc:
        assert expected_message in str(exc)
    else:
        raise AssertionError("provider sport publication unexpectedly succeeded")


def test_curated_publication_rejects_contextmanager_runtime_type_rebind_before_dispatch(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    witness = _public_witness()
    factory_globals = witness[2]
    canonical_contextmanager_type = witness[7]
    assert factory_globals.get("_GeneratorContextManager") is canonical_contextmanager_type
    hostile_calls = 0

    class HostileContextManager:
        def __init__(self, *_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1

        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    factory_globals["_GeneratorContextManager"] = HostileContextManager
    try:
        _assert_publication_fails_closed(
            registry,
            "contextmanager runtime authority changed",
        )
    finally:
        factory_globals["_GeneratorContextManager"] = canonical_contextmanager_type

    assert hostile_calls == 0
    assert registry.bindings == ()


def test_curated_publication_rejects_contextmanager_enter_rebind_before_dispatch(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    witness = _public_witness()
    contextmanager_surface = witness[9]
    enter_entry = next(
        entry for entry in contextmanager_surface if entry[0] == "__enter__"
    )
    _name, owner, canonical_enter, _canonical_enter_code = enter_entry
    hostile_calls = 0

    def hostile_enter(self):
        nonlocal hostile_calls
        hostile_calls += 1
        return canonical_enter(self)

    setattr(owner, "__enter__", hostile_enter)
    try:
        _assert_publication_fails_closed(
            registry,
            "contextmanager runtime authority changed: __enter__",
        )
    finally:
        setattr(owner, "__enter__", canonical_enter)

    assert hostile_calls == 0
    assert registry.bindings == ()


def test_curated_publication_rejects_guarded_contextmanager_delegate_retarget_before_dispatch(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    witness = _public_witness()
    factory = witness[0]
    trusted_body = witness[5]
    factory_closure = factory.__closure__
    assert factory_closure is not None
    delegate_cell = next(
        cell for cell in factory_closure if cell.cell_contents is trusted_body
    )
    hostile_calls = 0

    def hostile_body(_path):
        nonlocal hostile_calls
        hostile_calls += 1
        yield

    delegate_cell.cell_contents = hostile_body
    try:
        _assert_publication_fails_closed(
            registry,
            "durable lock wrapper delegation authority changed",
        )
    finally:
        delegate_cell.cell_contents = trusted_body

    assert hostile_calls == 0
    assert registry.bindings == ()
