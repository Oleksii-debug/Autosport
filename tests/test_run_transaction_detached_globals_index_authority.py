from __future__ import annotations

from types import FunctionType

from autosport.run_transaction import RunTransaction


def _closure_cell(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)]


def _closure_value(function: FunctionType, name: str):
    return _closure_cell(function, name).cell_contents


def test_guard_rejects_coordinated_inner_globals_index_and_cell_retarget() -> None:
    """The verifier cannot move the slot that receives fresh per-call globals."""

    guarded = RunTransaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)
    clone = _closure_value(guarded, "function")
    require_bindings = _closure_value(guarded, "require_bindings")
    assert isinstance(clone, FunctionType)
    assert isinstance(require_bindings, FunctionType)
    assert clone.__closure__ is not None

    guard_index_cell = _closure_cell(guarded, "inner_globals_index")
    checker_index_cell = _closure_cell(require_bindings, "inner_globals_index")
    checker_globals_cell_cell = _closure_cell(require_bindings, "inner_globals_cell")

    original_guard_index = guard_index_cell.cell_contents
    original_checker_index = checker_index_cell.cell_contents
    original_checker_globals_cell = checker_globals_cell_cell.cell_contents
    assert original_guard_index == original_checker_index
    assert clone.__code__.co_freevars[original_guard_index] == "inner_globals"
    original_executed_globals = clone.__closure__[original_guard_index].cell_contents
    assert type(original_executed_globals) is dict

    decoy_index = next(
        index
        for index in range(len(clone.__closure__))
        if index != original_guard_index
    )
    decoy_cell = clone.__closure__[decoy_index]
    original_decoy_value = decoy_cell.cell_contents

    # Point both verifier expectations at a different real closure cell and give that
    # cell the expected globals identity.  The clone's true inner_globals cell remains
    # untouched.  If admitted, per-call reconstruction writes call_globals into the
    # decoy slot and leaves the true executable inner_globals slot on its reachable
    # diagnostic dictionary rather than the frozen per-call snapshot.
    decoy_cell.cell_contents = original_executed_globals
    guard_index_cell.cell_contents = decoy_index
    checker_index_cell.cell_contents = decoy_index
    checker_globals_cell_cell.cell_contents = decoy_cell
    try:
        try:
            guarded()
        except Exception as exc:  # noqa: BLE001 - rejection must precede delegate binding.
            assert not isinstance(exc, TypeError), (
                "guard admitted coordinated inner-globals index/cell retargeting far enough "
                "to invoke delegated argument binding"
            )
        else:
            raise AssertionError(
                "guard unexpectedly returned after coordinated inner-globals slot retargeting"
            )
    finally:
        checker_globals_cell_cell.cell_contents = original_checker_globals_cell
        checker_index_cell.cell_contents = original_checker_index
        guard_index_cell.cell_contents = original_guard_index
        decoy_cell.cell_contents = original_decoy_value

    assert guard_index_cell.cell_contents == original_guard_index
    assert checker_index_cell.cell_contents == original_checker_index
    assert checker_globals_cell_cell.cell_contents is original_checker_globals_cell
    assert clone.__closure__[original_guard_index].cell_contents is original_executed_globals
