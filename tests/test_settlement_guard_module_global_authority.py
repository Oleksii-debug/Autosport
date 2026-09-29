from __future__ import annotations

import builtins

import pytest

import autosport._continuous_session_settlement_dispatch_guard as settlement_guard
from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
)


def _load_book_target():
    coordinator = ContinuousSessionCoordinator
    coordinator_dict = type.__getattribute__(coordinator, "__dict__")
    descriptor = coordinator_dict["_load_book"]
    descriptor_get = type(descriptor).__dict__["__get__"]
    return coordinator, descriptor, descriptor_get(descriptor, None, coordinator)


def test_economic_entry_ignores_guard_global_getattr_shadow() -> None:
    """A forged guard global cannot hide an in-place helper executable mutation."""

    coordinator, _descriptor, target = _load_book_target()
    original_code = target.__code__
    had_global = "getattr" in settlement_guard.__dict__
    original_global = settlement_guard.__dict__.get("getattr")
    calls = 0

    def forged_getattr(obj, name, *default):
        nonlocal calls
        if obj is target and name == "__code__":
            calls += 1
            return original_code
        return builtins.getattr(obj, name, *default)

    def hostile(*_args, **_kwargs):
        raise AssertionError("mutated sealed settlement helper reached")

    instance = object.__new__(coordinator)
    try:
        target.__code__ = hostile.__code__
        settlement_guard.getattr = forged_getattr
        assert settlement_guard.getattr(target, "__code__", None) is original_code
        assert calls == 1
        with pytest.raises(
            ContinuousSessionError,
            match="settlement coordinator dispatch changed",
        ):
            _ = instance._settle
        assert calls == 1
    finally:
        target.__code__ = original_code
        if had_global:
            settlement_guard.getattr = original_global
        else:
            settlement_guard.__dict__.pop("getattr", None)


def test_metaclass_guard_ignores_guard_global_issubclass_shadow() -> None:
    """A forged guard global cannot authorize protected coordinator replacement."""

    coordinator, descriptor, _target = _load_book_target()
    had_global = "issubclass" in settlement_guard.__dict__
    original_global = settlement_guard.__dict__.get("issubclass")
    settlement_guard.issubclass = lambda *_args: False
    try:
        assert settlement_guard.issubclass(coordinator, coordinator) is False
        with pytest.raises(
            TypeError,
            match="canonical settlement coordinator dispatch is immutable",
        ):
            setattr(coordinator, "_load_book", lambda _self: None)
        current = type.__getattribute__(coordinator, "__dict__")["_load_book"]
        assert current is descriptor
    finally:
        current = type.__getattribute__(coordinator, "__dict__")["_load_book"]
        if current is not descriptor:
            type.__setattr__(coordinator, "_load_book", descriptor)
        if had_global:
            settlement_guard.issubclass = original_global
        else:
            settlement_guard.__dict__.pop("issubclass", None)
