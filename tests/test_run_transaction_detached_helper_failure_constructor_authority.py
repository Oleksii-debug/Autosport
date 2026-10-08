from __future__ import annotations

import builtins
from types import FunctionType

import autosport._run_transaction_detached_verifier_helper_guard as helper_guard
from autosport.run_transaction import RunTransaction


def _closure_cell(function: FunctionType, name: str):
    closure = function.__closure__
    freevars = function.__code__.co_freevars
    assert closure is not None
    assert name in freevars
    return closure[freevars.index(name)]


def _sealed_helper(guarded: FunctionType) -> FunctionType:
    helper = _closure_cell(guarded, "require_bindings").cell_contents
    assert isinstance(helper, FunctionType)
    assert "original_dict_get_cell" in helper.__code__.co_freevars
    return helper


def test_detached_helper_rejection_does_not_execute_shadowed_failure_constructor() -> None:
    """Helper authority failure must use its composition-time failure type."""

    guarded = RunTransaction._stage_paper_book_snapshot
    assert isinstance(guarded, FunctionType)
    sealed = _sealed_helper(guarded)
    helper_cell = _closure_cell(sealed, "original_dict_get_cell").cell_contents
    original_helper = helper_cell.cell_contents
    assert original_helper is dict.get

    had_failure_type = "ValueError" in helper_guard.__dict__
    previous_failure_type = helper_guard.__dict__.get("ValueError")
    hostile_calls = 0

    def hostile_value_error(*args, **kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        return builtins.ValueError(*args, **kwargs)

    helper_cell.cell_contents = object()
    helper_guard.ValueError = hostile_value_error
    try:
        try:
            guarded(object())
        except Exception:  # noqa: BLE001 - fail closed is the required oracle.
            pass
        else:
            raise AssertionError("mutated detached helper authority did not fail closed")
    finally:
        helper_cell.cell_contents = original_helper
        if had_failure_type:
            helper_guard.ValueError = previous_failure_type
        else:
            helper_guard.__dict__.pop("ValueError", None)

    assert hostile_calls == 0, "detached helper dispatched shadowed ValueError"
