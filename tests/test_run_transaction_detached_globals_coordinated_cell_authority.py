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


def test_binding_checker_rejects_coordinated_clone_verifier_and_anchor_retarget() -> None:
    """Clone execution, verifier expectation and identity anchor must not be jointly retargetable."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    clone = _closure_value(guarded, "function")
    require_bindings = _closure_value(guarded, "require_bindings")
    assert isinstance(clone, FunctionType)
    assert isinstance(require_bindings, FunctionType)

    executed_globals_cell = _closure_cell(clone, "inner_globals")
    verifier_globals_cell = _closure_cell(require_bindings, "inner_globals")
    anchor_cell = _closure_cell(require_bindings, "inner_globals_anchor")

    original_executed_globals = executed_globals_cell.cell_contents
    original_verifier_globals = verifier_globals_cell.cell_contents
    original_anchor = anchor_cell.cell_contents
    assert type(original_executed_globals) is dict
    assert original_verifier_globals is original_executed_globals
    assert type(original_anchor) is tuple
    assert len(original_anchor) == 1
    assert original_anchor[0] is original_executed_globals
    assert executed_globals_cell is not verifier_globals_cell

    replacement_globals = dict(original_executed_globals)
    assert replacement_globals == original_executed_globals
    assert replacement_globals is not original_executed_globals

    executed_globals_cell.cell_contents = replacement_globals
    verifier_globals_cell.cell_contents = replacement_globals
    anchor_cell.cell_contents = (replacement_globals,)
    try:
        try:
            require_bindings()
        except Exception:  # noqa: BLE001 - any fail-closed rejection satisfies this oracle.
            pass
        else:
            raise AssertionError(
                "binding checker accepted coordinated retargeting of the clone's executed "
                "globals cell, verifier expected-globals cell, and writable anchor cell"
            )
    finally:
        anchor_cell.cell_contents = original_anchor
        verifier_globals_cell.cell_contents = original_verifier_globals
        executed_globals_cell.cell_contents = original_executed_globals

    assert executed_globals_cell.cell_contents is original_executed_globals
    assert verifier_globals_cell.cell_contents is original_verifier_globals
    assert anchor_cell.cell_contents is original_anchor
