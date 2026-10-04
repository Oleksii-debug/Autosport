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


def test_surface_checker_rejects_coordinated_root_and_witness_snapshot_retarget() -> None:
    """The terminal checker cannot approve a hostile surface via a moved expectation."""

    guarded = RunTransaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)
    require_surface = _closure_value(guarded, "require_surface")
    assert isinstance(require_surface, FunctionType)
    surface_authority = _closure_value(require_surface, "surface_authority")
    assert isinstance(surface_authority, FunctionType)

    witness_cell = _closure_cell(surface_authority, "frozen_witnesses")
    surface_type = _closure_value(surface_authority, "surface_type")
    original_witnesses = witness_cell.cell_contents
    assert isinstance(original_witnesses, tuple)
    assert original_witnesses

    # Reproduce the already-supported finite metaclass-peel threat model.  The
    # positive dispatch facade itself is made hostile while the terminal checker
    # function object and its code remain untouched.
    surface_meta = type(surface_type)
    surface_meta_meta = type(surface_meta)
    original_surface_root = vars(surface_type)["__getattr__"]
    original_meta_root = vars(surface_meta)["__getattr__"]
    original_meta_meta_root = vars(surface_meta_meta)["__getattr__"]
    hostile_replace = lambda *_args: None

    def hostile_getattr(_surface, name):
        if name == "replace":
            return hostile_replace
        raise AttributeError(name)

    try:
        type.__setattr__(surface_meta_meta, "__getattr__", object())
        type.__setattr__(surface_meta, "__getattr__", object())
        type.__setattr__(surface_type, "__getattr__", hostile_getattr)

        # Move only the checker's expected witness snapshot to the now-hostile
        # current roots.  A code-only checker witness must not let this coordinated
        # state+expectation retarget become authority.
        current_namespace = vars(surface_type)
        moved_witnesses = tuple(
            (
                name,
                current_namespace[name],
                current_namespace[name].__code__
                if type(current_namespace[name]) is FunctionType
                else None,
            )
            for name, _expected, _expected_code in original_witnesses
        )
        witness_cell.cell_contents = moved_witnesses

        try:
            require_surface()
        except Exception:  # noqa: BLE001 - any fail-closed rejection satisfies the oracle.
            pass
        else:
            raise AssertionError(
                "surface checker approved hostile roots after its witness snapshot moved"
            )
    finally:
        witness_cell.cell_contents = original_witnesses
        # Restore from the governed class outward while each upper seal is inert.
        type.__setattr__(surface_type, "__getattr__", original_surface_root)
        type.__setattr__(surface_meta, "__getattr__", original_meta_root)
        type.__setattr__(surface_meta_meta, "__getattr__", original_meta_meta_root)

    assert witness_cell.cell_contents is original_witnesses
    assert vars(surface_type)["__getattr__"] is original_surface_root
