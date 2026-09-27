from __future__ import annotations

import pytest

import autosport.provider_sport_mapping as mapping


class _HostileRegistry(mapping.ProviderSportMappingRegistry):
    calls: list[str] = []

    def __getattribute__(self, name: str):
        type(self).calls.append(name)
        return super().__getattribute__(name)


def test_curated_publication_rejects_registry_subclass_before_instance_dispatch() -> None:
    hostile = object.__new__(_HostileRegistry)
    _HostileRegistry.calls = []

    with pytest.raises(
        mapping.ProviderSportMappingError,
        match="canonical provider sport registry type|registry type|authority",
    ):
        mapping.ProviderSportMappingRegistry.register_curated(
            hostile,
            provider_namespace="betfair",
            provider_sport_id="1",
        )

    assert _HostileRegistry.calls == []
