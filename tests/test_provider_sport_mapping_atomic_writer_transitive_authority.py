from __future__ import annotations

from pathlib import Path

import pytest

import autosport.provider_sport_mapping as mapping


def test_curated_registration_rejects_self_restoring_os_replace_substitution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "provider-sport-map.json"
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(path)

    writer_globals = mapping.atomic_write_json.__globals__
    os_module = writer_globals["os"]
    original_replace = os_module.replace
    hostile_called = False

    def hostile_replace(source, destination):
        nonlocal hostile_called
        del source, destination
        hostile_called = True
        os_module.replace = original_replace
        # Returning without publication previously let register_curated return a
        # positive binding while the durable registry remained pristine.
        return None

    os_module.replace = hostile_replace
    try:
        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="writer dependency authority",
        ):
            registry.register_curated(
                provider_namespace="betfair",
                provider_sport_id="1",
            )
    finally:
        os_module.replace = original_replace

    assert hostile_called is False
    assert registry.bindings == ()
    restarted = mapping.ProviderSportMappingRegistry(path)
    assert restarted.bindings == ()
