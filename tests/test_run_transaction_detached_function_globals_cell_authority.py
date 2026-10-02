from __future__ import annotations

from types import FunctionType

import pytest

import autosport  # noqa: F401 - package composition installs the guard
from autosport.run_transaction import RunTransaction


def _closure_cell(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)]


def test_detached_function_globals_snapshot_cell_cannot_mint_dispatch() -> None:
    """Retargeting the execution snapshot cell must fail before hostile dispatch."""

    guarded = RunTransaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    snapshot_cell = _closure_cell(guarded, "frozen_function_globals_items")
    original_snapshot = snapshot_cell.cell_contents
    assert type(original_snapshot) is tuple
    assert "dict" not in dict(original_snapshot)

    hostile_calls: list[object] = []

    def hostile_dict(*_args, **_kwargs):
        hostile_calls.append(object())
        raise AssertionError(
            "retargeted detached function-globals snapshot reached execution"
        )

    poisoned_snapshot = original_snapshot + (("dict", hostile_dict),)
    snapshot_cell.cell_contents = poisoned_snapshot
    try:
        with pytest.raises((RuntimeError, TypeError, ValueError)):
            guarded(object())
    finally:
        snapshot_cell.cell_contents = original_snapshot

    assert hostile_calls == []
    assert snapshot_cell.cell_contents is original_snapshot
