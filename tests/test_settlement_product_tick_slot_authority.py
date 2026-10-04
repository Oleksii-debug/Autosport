from __future__ import annotations

import pytest

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
)
from autosport.continuous_session_entry import tick_continuous_session


def test_direct_base_type_tick_replacement_cannot_bypass_product_preflight() -> None:
    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    original_tick = coordinator_dict["tick"]
    hostile_calls = 0

    def hostile_tick(self):
        nonlocal hostile_calls
        del self
        hostile_calls += 1
        return object()

    coordinator = coordinator_type.__new__(coordinator_type)
    try:
        # This is the same base-type bypass already exercised against `_settle` and
        # protected helper slots. Replacing the compatibility wrapper entry itself
        # must not remove its class-level lookup preflight.
        type.__setattr__(coordinator_type, "tick", hostile_tick)
        with pytest.raises(ContinuousSessionError):
            coordinator.tick()
        assert hostile_calls == 0
    finally:
        type.__setattr__(coordinator_type, "tick", original_tick)


def test_product_entry_does_not_depend_on_replaceable_instance_lookup_root() -> None:
    coordinator_type = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator_type, "__dict__")
    original_getattribute = coordinator_dict["__getattribute__"]
    hostile_calls = 0

    def hostile_tick(self):
        nonlocal hostile_calls
        del self
        hostile_calls += 1
        return object()

    def hostile_getattribute(self, name):
        if name == "tick":
            return hostile_tick.__get__(self, coordinator_type)
        return object.__getattribute__(self, name)

    coordinator = coordinator_type.__new__(coordinator_type)
    try:
        # Arbitrary base-type replacement of __getattribute__ happens before an
        # expression such as coordinator.tick() can execute any Python guard.  The
        # supported authority-bearing product entry therefore calls the exact captured
        # guarded tick non-virtually and must reject the changed lookup slot first.
        type.__setattr__(
            coordinator_type,
            "__getattribute__",
            hostile_getattribute,
        )
        with pytest.raises(ContinuousSessionError):
            tick_continuous_session(coordinator)
        assert hostile_calls == 0
    finally:
        type.__setattr__(
            coordinator_type,
            "__getattribute__",
            original_getattribute,
        )
