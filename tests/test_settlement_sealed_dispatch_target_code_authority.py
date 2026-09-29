from __future__ import annotations

import pytest

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
)


def test_economic_entry_rejects_in_place_sealed_helper_executable_mutation() -> None:
    """Descriptor identity alone must not authorize a mutated hidden helper target."""

    coordinator = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator, "__dict__")
    descriptor = coordinator_dict["_load_book"]
    descriptor_get = type(descriptor).__dict__["__get__"]
    target = descriptor_get(descriptor, None, coordinator)
    original_code = target.__code__

    def hostile(*_args, **_kwargs):
        raise AssertionError("mutated sealed settlement helper reached")

    instance = object.__new__(coordinator)
    try:
        target.__code__ = hostile.__code__
        with pytest.raises(
            ContinuousSessionError,
            match="settlement coordinator dispatch changed",
        ):
            _ = instance._settle
    finally:
        target.__code__ = original_code
