from __future__ import annotations

from types import FunctionType

import autosport.run_transaction as run_transaction


def _closure_cell(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)]


def _no_op_code_with_same_freevars(function: FunctionType):
    names = function.__code__.co_freevars
    parameters = ", ".join(names)
    references = ", ".join(names)
    namespace: dict[str, object] = {}
    if names:
        source = (
            f"def outer({parameters}):\n"
            "    def hostile():\n"
            f"        captured = ({references},)\n"
            "        del captured\n"
            "        return None\n"
            "    return hostile\n"
        )
        exec(source, namespace)
        outer = namespace["outer"]
        assert isinstance(outer, FunctionType)
        hostile = outer(*([None] * len(names)))
    else:
        def hostile():
            return None
    assert isinstance(hostile, FunctionType)
    assert hostile.__code__.co_freevars == names
    return hostile.__code__


def test_surface_verifier_rejects_coordinated_checker_code_retarget() -> None:
    """The terminal frozen-surface checker cannot move with its expected-code cell."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)
    require_surface = _closure_cell(guarded, "require_surface").cell_contents
    assert isinstance(require_surface, FunctionType)

    checker_cell = _closure_cell(require_surface, "surface_authority")
    checker_code_cell = _closure_cell(require_surface, "surface_authority_code")
    checker = checker_cell.cell_contents
    original_code = checker_code_cell.cell_contents
    assert isinstance(checker, FunctionType)
    assert checker.__code__ is original_code

    hostile_code = _no_op_code_with_same_freevars(checker)
    assert hostile_code is not original_code

    checker.__code__ = hostile_code
    checker_code_cell.cell_contents = hostile_code
    try:
        try:
            require_surface()
        except Exception:  # noqa: BLE001 - any fail-closed rejection satisfies the oracle.
            pass
        else:
            raise AssertionError(
                "surface verifier accepted coordinated checker/expected-code retargeting"
            )
    finally:
        checker_code_cell.cell_contents = original_code
        checker.__code__ = original_code

    assert checker.__code__ is original_code
    assert checker_code_cell.cell_contents is original_code
