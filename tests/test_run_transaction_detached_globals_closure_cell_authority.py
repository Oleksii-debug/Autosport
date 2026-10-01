from __future__ import annotations

from types import FunctionType

import autosport.run_transaction as run_transaction


def _closure_cell(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)]


def _closure_value(function: FunctionType, name: str):
    return _closure_cell(function, name).cell_contents


def test_guarded_promotion_rejects_replaced_executed_globals_cell_before_delegate() -> None:
    """The guard must witness the exact closure cell the detached clone executes.

    A separately captured ``inner_globals`` dictionary is not sufficient authority:
    Python closure cells are writable.  Replacing only the clone's cell must be
    rejected by the guard before the underlying promotion function is entered.
    Calling the guarded consumer without its business arguments deliberately
    gives us a deterministic oracle: a ``TypeError`` means the guard admitted
    the mutated clone far enough to invoke the delegated function.
    """

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    clone = _closure_value(guarded, "function")
    assert isinstance(clone, FunctionType)

    executed_globals_cell = _closure_cell(clone, "inner_globals")
    original_globals = executed_globals_cell.cell_contents
    assert type(original_globals) is dict

    replacement_globals = dict(original_globals)
    assert replacement_globals == original_globals
    assert replacement_globals is not original_globals

    executed_globals_cell.cell_contents = replacement_globals
    try:
        try:
            guarded()
        except Exception as exc:  # noqa: BLE001 - the invariant is pre-delegate rejection.
            assert not isinstance(exc, TypeError), (
                "detached promotion admitted a replaced inner_globals closure cell "
                "and reached delegated argument binding"
            )
        else:
            raise AssertionError(
                "detached promotion unexpectedly returned after inner_globals closure replacement"
            )
    finally:
        executed_globals_cell.cell_contents = original_globals

    assert executed_globals_cell.cell_contents is original_globals
