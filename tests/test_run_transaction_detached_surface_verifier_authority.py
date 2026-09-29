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


def test_guard_rejects_coordinated_surface_verifier_and_code_retarget() -> None:
    """Terminal surface verifier and its expected code cannot be jointly replaced."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)
    require_surface = _closure_value(guarded, "require_surface")
    assert isinstance(require_surface, FunctionType)

    verifier_cell = _closure_cell(require_surface, "surface_authority")
    verifier_code_cell = _closure_cell(require_surface, "surface_authority_code")
    original_verifier = verifier_cell.cell_contents
    original_code = verifier_code_cell.cell_contents
    assert isinstance(original_verifier, FunctionType)
    assert original_verifier.__code__ is original_code

    def bypass_surface_authority() -> None:
        return None

    verifier_cell.cell_contents = bypass_surface_authority
    verifier_code_cell.cell_contents = bypass_surface_authority.__code__
    try:
        try:
            guarded()
        except Exception as exc:  # noqa: BLE001 - rejection must precede delegate binding.
            assert not isinstance(exc, TypeError), (
                "guard admitted coordinated surface-verifier/code retargeting far enough "
                "to invoke delegated argument binding"
            )
        else:
            raise AssertionError(
                "guard unexpectedly returned after coordinated surface-verifier retargeting"
            )
    finally:
        verifier_code_cell.cell_contents = original_code
        verifier_cell.cell_contents = original_verifier

    assert verifier_cell.cell_contents is original_verifier
    assert verifier_code_cell.cell_contents is original_code
