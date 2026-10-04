from __future__ import annotations

import builtins
from types import FunctionType

import autosport._run_transaction_paperbook_direct_dispatch_guard as direct_guard
from autosport.run_transaction import RunTransaction


def _closure_cell(function: FunctionType, name: str):
    closure = function.__closure__
    freevars = function.__code__.co_freevars
    assert closure is not None
    assert name in freevars
    return closure[freevars.index(name)]


def test_nested_direct_dispatch_verifier_does_not_execute_shadowed_failure_constructor() -> None:
    method = RunTransaction._stage_paper_book_snapshot
    assert isinstance(method, FunctionType)
    inner_globals = _closure_cell(method, "inner_globals").cell_contents
    assert type(inner_globals) is dict
    original_owner = inner_globals["RunTransaction"]
    had_failure_type = "ValueError" in direct_guard.__dict__
    previous_failure_type = direct_guard.__dict__.get("ValueError")
    hostile_calls = 0

    def hostile_value_error(*args, **kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        return builtins.ValueError(*args, **kwargs)

    inner_globals["RunTransaction"] = object()
    direct_guard.ValueError = hostile_value_error
    try:
        try:
            method(object())
        except Exception:  # noqa: BLE001 - fail closed is required.
            pass
        else:
            raise AssertionError("mutated direct-dispatch binding did not fail closed")
    finally:
        inner_globals["RunTransaction"] = original_owner
        if had_failure_type:
            direct_guard.ValueError = previous_failure_type
        else:
            direct_guard.__dict__.pop("ValueError", None)

    assert hostile_calls == 0, "nested verifier dispatched shadowed ValueError"


def test_outer_direct_dispatch_verifier_does_not_execute_shadowed_failure_constructor() -> None:
    method = RunTransaction._stage_paper_book_snapshot
    assert isinstance(method, FunctionType)
    function_cell = _closure_cell(method, "function")
    original_function = function_cell.cell_contents
    assert isinstance(original_function, FunctionType)
    had_failure_type = "ValueError" in direct_guard.__dict__
    previous_failure_type = direct_guard.__dict__.get("ValueError")
    hostile_calls = 0

    def hostile_value_error(*args, **kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        return builtins.ValueError(*args, **kwargs)

    function_cell.cell_contents = object()
    direct_guard.ValueError = hostile_value_error
    try:
        try:
            method(object())
        except Exception:  # noqa: BLE001 - fail closed is required.
            pass
        else:
            raise AssertionError("mutated direct-dispatch executable did not fail closed")
    finally:
        function_cell.cell_contents = original_function
        if had_failure_type:
            direct_guard.ValueError = previous_failure_type
        else:
            direct_guard.__dict__.pop("ValueError", None)

    assert hostile_calls == 0, "outer verifier dispatched shadowed ValueError"
