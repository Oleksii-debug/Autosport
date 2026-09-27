from __future__ import annotations

from pathlib import Path

import autosport.provider_sport_mapping as mapping


def test_curated_registration_ignores_sorted_builtin_substitution(
    tmp_path: Path,
) -> None:
    """A self-restoring builtin cannot divert product-curated durable publication."""

    path = tmp_path / "provider-sport-map.json"
    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(path)
    guarded_register = mapping.ProviderSportMappingRegistry.register_curated
    builtins_map = guarded_register.__builtins__
    original_sorted = builtins_map["sorted"]
    hostile_called = False

    def hostile_sorted(iterable, *, key=None, reverse=False):
        nonlocal hostile_called
        del iterable, key, reverse
        hostile_called = True
        builtins_map["sorted"] = original_sorted
        return []

    builtins_map["sorted"] = hostile_sorted
    try:
        binding = registry.register_curated(
            provider_namespace="betfair",
            provider_sport_id="1",
        )
    finally:
        builtins_map["sorted"] = original_sorted

    assert hostile_called is False
    assert binding.canonical_sport == "football"
    assert registry.bindings == (binding,)

    restarted = mapping.ProviderSportMappingRegistry(path)
    assert restarted.bindings == (binding,)
    resolution = restarted.resolve(
        provider_namespace="betfair",
        provider_sport_id="1",
        as_of=binding.recorded_at,
    )
    assert resolution.binding_id == binding.binding_id
    assert resolution.canonical_sport == "football"
