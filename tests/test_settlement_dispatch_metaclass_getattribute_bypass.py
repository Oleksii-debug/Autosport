from __future__ import annotations

import pytest

from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
)


def test_economic_entry_does_not_trust_mutable_metaclass_dict_dispatch() -> None:
    """A forged metaclass ``__getattribute__`` must not hide helper replacement."""

    coordinator = ContinuousSessionCoordinator
    metaclass = type(coordinator)
    real_class_dict = type.__getattribute__(coordinator, "__dict__")
    original_helper = real_class_dict["_load_book"]
    metaclass_dict = type.__getattribute__(metaclass, "__dict__")
    original_meta_getattribute = metaclass_dict.get("__getattribute__")

    forged_view = dict(real_class_dict)

    def hostile_meta_getattribute(cls, name: str):
        if cls is coordinator and name == "__dict__":
            return forged_view
        return type.__getattribute__(cls, name)

    instance = object.__new__(coordinator)
    try:
        type.__setattr__(
            coordinator,
            "_load_book",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("forged settlement book dispatch reached")
            ),
        )
        type.__setattr__(metaclass, "__getattribute__", hostile_meta_getattribute)

        # A witness that late-reads ``coordinator.__dict__`` would now see the forged
        # pre-mutation mapping and incorrectly authorize the economic entry.
        assert coordinator.__dict__["_load_book"] is original_helper
        assert type.__getattribute__(coordinator, "__dict__")["_load_book"] is not original_helper

        with pytest.raises(
            ContinuousSessionError,
            match="settlement coordinator dispatch changed",
        ):
            _ = instance._settle
    finally:
        type.__setattr__(coordinator, "_load_book", original_helper)
        if original_meta_getattribute is None:
            type.__delattr__(metaclass, "__getattribute__")
        else:
            type.__setattr__(metaclass, "__getattribute__", original_meta_getattribute)
