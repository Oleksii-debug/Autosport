from __future__ import annotations

from pathlib import Path

import pytest

import autosport.provider_sport_mapping as mapping


def test_curated_registration_cannot_return_positive_binding_after_sorted_builtin_substitution(
    tmp_path: Path,
) -> None:
    """A self-restoring builtin cannot make positive curation skip durable publication."""

    registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "provider-sport-map.json"
    )
    guarded_register = mapping.ProviderSportMappingRegistry.register_curated
    builtins_map = guarded_register.__builtins__
    original_sorted = builtins_map["sorted"]
    hostile_called = False

    def hostile_sorted(iterable, *, key=None, reverse=False):
        nonlocal hostile_called
        del iterable, key, reverse
        hostile_called = True
        # Restore before the guard's post-sort authority recheck. The positive path
        # must still fail closed rather than persist an empty registry and return a
        # product-curated binding that durable readback cannot reproduce.
        builtins_map["sorted"] = original_sorted
        return []

    builtins_map["sorted"] = hostile_sorted
    try:
        with pytest.raises(mapping.ProviderSportMappingError):
            registry.register_curated(
                provider_namespace="betfair",
                provider_sport_id="1",
            )
    finally:
        builtins_map["sorted"] = original_sorted

    assert hostile_called is True
    assert registry.bindings == ()
