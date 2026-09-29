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


def test_guard_rejects_coordinated_execution_snapshot_and_anchor_retarget() -> None:
    """Execution snapshot and its writable tuple anchor cannot move together."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    snapshot_cell = _closure_cell(guarded, "frozen_globals_items")
    anchor_cell = _closure_cell(guarded, "frozen_globals_items_anchor")
    original_snapshot = snapshot_cell.cell_contents
    original_anchor = anchor_cell.cell_contents
    assert type(original_snapshot) is tuple
    assert type(original_anchor) is tuple
    assert len(original_anchor) == 1
    assert original_anchor[0] is original_snapshot

    replacement_snapshot = tuple(list(original_snapshot))
    assert replacement_snapshot == original_snapshot
    assert replacement_snapshot is not original_snapshot

    snapshot_cell.cell_contents = replacement_snapshot
    anchor_cell.cell_contents = (replacement_snapshot,)
    try:
        try:
            guarded()
        except Exception as exc:  # noqa: BLE001 - rejection must happen before delegate binding.
            assert not isinstance(exc, TypeError), (
                "guard admitted coordinated execution-snapshot and anchor retargeting "
                "far enough to invoke delegated argument binding"
            )
        else:
            raise AssertionError(
                "guard unexpectedly returned after coordinated execution-snapshot retargeting"
            )
    finally:
        anchor_cell.cell_contents = original_anchor
        snapshot_cell.cell_contents = original_snapshot

    assert snapshot_cell.cell_contents is original_snapshot
    assert anchor_cell.cell_contents is original_anchor


def test_guard_rejects_coordinated_binding_verifier_and_snapshot_retarget() -> None:
    """The verifier function/code pair cannot move with the execution snapshot."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    verifier_cell = _closure_cell(guarded, "require_bindings")
    verifier_code_cell = _closure_cell(guarded, "require_bindings_code")
    snapshot_cell = _closure_cell(guarded, "frozen_globals_items")
    anchor_cell = _closure_cell(guarded, "frozen_globals_items_anchor")

    original_verifier = verifier_cell.cell_contents
    original_verifier_code = verifier_code_cell.cell_contents
    original_snapshot = snapshot_cell.cell_contents
    original_anchor = anchor_cell.cell_contents
    assert isinstance(original_verifier, FunctionType)
    assert original_verifier.__code__ is original_verifier_code
    assert type(original_snapshot) is tuple
    assert type(original_anchor) is tuple
    assert original_anchor == (original_snapshot,)

    replacement_snapshot = tuple(list(original_snapshot))
    assert replacement_snapshot == original_snapshot
    assert replacement_snapshot is not original_snapshot

    def bypass_bindings() -> None:
        return None

    verifier_cell.cell_contents = bypass_bindings
    verifier_code_cell.cell_contents = bypass_bindings.__code__
    snapshot_cell.cell_contents = replacement_snapshot
    anchor_cell.cell_contents = (replacement_snapshot,)
    try:
        try:
            guarded()
        except Exception as exc:  # noqa: BLE001 - rejection must precede delegate binding.
            assert not isinstance(exc, TypeError), (
                "guard admitted coordinated verifier/code and execution-snapshot retargeting "
                "far enough to invoke delegated argument binding"
            )
        else:
            raise AssertionError(
                "guard unexpectedly returned after coordinated verifier/code retargeting"
            )
    finally:
        anchor_cell.cell_contents = original_anchor
        snapshot_cell.cell_contents = original_snapshot
        verifier_code_cell.cell_contents = original_verifier_code
        verifier_cell.cell_contents = original_verifier

    assert verifier_cell.cell_contents is original_verifier
    assert verifier_code_cell.cell_contents is original_verifier_code
    assert snapshot_cell.cell_contents is original_snapshot
    assert anchor_cell.cell_contents is original_anchor


def test_guard_rejects_coordinated_type_and_dict_primitive_retarget() -> None:
    """Type validation and execution-map construction cannot be retargeted together."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    exact_type_cell = _closure_cell(guarded, "exact_type")
    dict_type_cell = _closure_cell(guarded, "dict_type")
    original_exact_type = exact_type_cell.cell_contents
    original_dict_type = dict_type_cell.cell_contents
    assert original_exact_type is type
    assert original_dict_type is dict

    calls = 0

    def hostile_dict(*args, **kwargs):
        nonlocal calls
        calls += 1
        return dict(*args, **kwargs)

    def discriminator(value):
        if type(value) is dict:
            return hostile_dict
        return type(value)

    exact_type_cell.cell_contents = discriminator
    dict_type_cell.cell_contents = hostile_dict
    try:
        try:
            guarded()
        except Exception as exc:  # noqa: BLE001 - rejection must precede map construction.
            assert not isinstance(exc, TypeError), (
                "guard admitted coordinated type/dict primitive retargeting far enough "
                "to invoke delegated argument binding"
            )
        else:
            raise AssertionError(
                "guard unexpectedly returned after coordinated primitive retargeting"
            )
        assert calls == 0, "hostile execution-map constructor was invoked"
    finally:
        dict_type_cell.cell_contents = original_dict_type
        exact_type_cell.cell_contents = original_exact_type

    assert exact_type_cell.cell_contents is original_exact_type
    assert dict_type_cell.cell_contents is original_dict_type
