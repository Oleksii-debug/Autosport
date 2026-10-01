from __future__ import annotations

from types import FunctionType

import autosport.run_transaction as run_transaction


def _closure_cell(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)]


def test_guard_rejects_coordinated_clone_code_and_expected_code_retarget() -> None:
    """Executable code and its mutable expected-code cell cannot move together."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    function_cell = _closure_cell(guarded, "function")
    inner_code_cell = _closure_cell(guarded, "inner_code")
    clone = function_cell.cell_contents
    original_expected_code = inner_code_cell.cell_contents
    assert isinstance(clone, FunctionType)
    assert clone.__code__ is original_expected_code

    replacement_code = original_expected_code.replace(
        co_name=f"{original_expected_code.co_name}_retargeted"
    )
    assert replacement_code is not original_expected_code
    assert replacement_code.co_freevars == original_expected_code.co_freevars

    clone.__code__ = replacement_code
    inner_code_cell.cell_contents = replacement_code
    try:
        try:
            guarded()
        except Exception as exc:  # noqa: BLE001 - rejection must precede delegate binding.
            assert not isinstance(exc, TypeError), (
                "guard admitted coordinated executable/expected-code retargeting far enough "
                "to invoke delegated argument binding"
            )
        else:
            raise AssertionError(
                "guard unexpectedly returned after coordinated executable-code retargeting"
            )
    finally:
        inner_code_cell.cell_contents = original_expected_code
        clone.__code__ = original_expected_code

    assert function_cell.cell_contents is clone
    assert clone.__code__ is original_expected_code
    assert inner_code_cell.cell_contents is original_expected_code
