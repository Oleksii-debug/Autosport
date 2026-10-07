from __future__ import annotations

import pytest

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
)


_PROTECTED = (
    "_settlement_resolutions",
    "_load_book",
    "_require_settlement_causal_for_open_tickets",
    "_open_quote_keys_for_book",
)


def test_settlement_authority_dispatch_rejects_instance_shadowing() -> None:
    instance = object.__new__(ContinuousSessionCoordinator)
    for name in _PROTECTED:
        with pytest.raises(TypeError, match="dispatch is immutable"):
            setattr(instance, name, lambda *args, **kwargs: None)


def test_settlement_authority_dispatch_rejects_class_rebinding_and_deletion() -> None:
    for name in _PROTECTED:
        with pytest.raises(TypeError, match="dispatch is immutable"):
            setattr(ContinuousSessionCoordinator, name, lambda *args, **kwargs: None)
        with pytest.raises(TypeError, match="dispatch is immutable"):
            delattr(ContinuousSessionCoordinator, name)


def test_dispatch_descriptor_has_no_caller_writable_target_state() -> None:
    descriptor = ContinuousSessionCoordinator.__dict__["_load_book"]
    with pytest.raises((AttributeError, TypeError)):
        descriptor._target = lambda *args, **kwargs: None
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(descriptor, "_target", lambda *args, **kwargs: None)


@pytest.mark.parametrize(
    "name",
    ("_open_quote_keys_for_book", "_load_book", "_settlement_resolutions"),
)
def test_base_type_setattr_helper_replacement_fails_closed_at_economic_entry(name: str) -> None:
    """Direct base-type mutation must be detected before settlement can execute.

    ``type.__setattr__`` can intentionally bypass a custom metaclass hook, so the
    authority invariant is fail-closed detection at the sealed economic entry rather
    than an impossible promise that the Python primitive itself cannot replace a slot.
    """

    original = ContinuousSessionCoordinator.__dict__[name]
    hostile = staticmethod(lambda *args, **kwargs: ("FORGED_SETTLEMENT_AUTHORITY",))
    instance = object.__new__(ContinuousSessionCoordinator)
    try:
        type.__setattr__(ContinuousSessionCoordinator, name, hostile)
        assert ContinuousSessionCoordinator.__dict__[name] is hostile
        with pytest.raises(
            ContinuousSessionError,
            match="settlement coordinator dispatch changed",
        ):
            _ = instance._settle
    finally:
        type.__setattr__(ContinuousSessionCoordinator, name, original)

    # Restoring the exact descriptor restores access to the canonical entry.
    assert callable(instance._settle)


def test_canonical_settlement_coordinator_cannot_be_subclassed() -> None:
    with pytest.raises(TypeError, match="not extensible"):
        class _HostileCoordinator(ContinuousSessionCoordinator):
            pass
