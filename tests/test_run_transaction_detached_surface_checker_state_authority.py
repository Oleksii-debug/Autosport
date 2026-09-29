from __future__ import annotations

from types import FunctionType

import autosport._paperbook_preload_authority_guard as paper_guard
import autosport.run_transaction as run_transaction


def _closure_cell(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)]


def _closure_value(function: FunctionType, name: str):
    return _closure_cell(function, name).cell_contents


def test_surface_checker_rejects_coordinated_expected_witness_retarget() -> None:
    """Terminal surface truth cannot move with the checker's writable expectation cell."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)
    require_surface = _closure_value(guarded, "require_surface")
    assert isinstance(require_surface, FunctionType)
    checker = _closure_value(require_surface, "surface_authority")
    assert isinstance(checker, FunctionType)

    witness_cell = _closure_cell(checker, "frozen_witnesses")
    original_witnesses = witness_cell.cell_contents
    assert type(original_witnesses) is tuple

    surface_type = type(paper_guard.os)
    surface_meta = type(surface_type)
    surface_meta_meta = type(surface_meta)
    original_surface_root = vars(surface_type)["__getattr__"]
    original_meta_root = vars(surface_meta)["__getattr__"]
    original_meta_meta_root = vars(surface_meta_meta)["__getattr__"]

    hostile_replace = lambda *_args: None

    def hostile_getattr(_surface, name: str):
        if name == "replace":
            return hostile_replace
        raise AttributeError(name)

    try:
        # Peel the finite heap-metaclass seal exactly as the existing terminal witness
        # regression does, then retarget only the checker's expected descriptor tuple.
        # The checker function/code, target type and all outer guard anchors stay intact.
        type.__setattr__(surface_meta_meta, "__getattr__", object())
        type.__setattr__(surface_meta, "__getattr__", object())
        type.__setattr__(surface_type, "__getattr__", hostile_getattr)
        assert paper_guard.os.replace is hostile_replace

        hostile_witnesses = tuple(
            (
                name,
                vars(surface_type)[name],
                vars(surface_type)[name].__code__
                if type(vars(surface_type)[name]) is FunctionType
                else None,
            )
            for name, _expected, _code in original_witnesses
        )
        witness_cell.cell_contents = hostile_witnesses

        try:
            require_surface()
        except Exception:  # noqa: BLE001 - any fail-closed rejection satisfies the oracle.
            pass
        else:
            raise AssertionError(
                "surface checker accepted hostile class state after its writable "
                "frozen_witnesses expectation was retargeted to match"
            )
    finally:
        witness_cell.cell_contents = original_witnesses
        type.__setattr__(surface_type, "__getattr__", original_surface_root)
        type.__setattr__(surface_meta, "__getattr__", original_meta_root)
        type.__setattr__(surface_meta_meta, "__getattr__", original_meta_meta_root)

    assert witness_cell.cell_contents is original_witnesses
    assert paper_guard.os.replace is not hostile_replace
