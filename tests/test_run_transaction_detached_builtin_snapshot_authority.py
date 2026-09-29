from __future__ import annotations

from types import FunctionType

import autosport.run_transaction as run_transaction


def _closure_value(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)].cell_contents


def test_detached_consumer_snapshots_builtins_as_inert_items() -> None:
    """Detached execution must not retain the mutable source ``__builtins__`` mapping."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    detached = _closure_value(guarded, "function")
    frozen_function_globals_items = _closure_value(
        guarded,
        "frozen_function_globals_items",
    )
    frozen_builtin_items = _closure_value(guarded, "frozen_builtin_items")

    assert isinstance(detached, FunctionType)
    assert type(frozen_function_globals_items) is tuple
    assert type(frozen_builtin_items) is tuple

    source_builtins = detached.__globals__.get("__builtins__")
    assert type(source_builtins) is dict
    assert "__builtins__" not in dict(frozen_function_globals_items)
    assert dict(frozen_builtin_items) == source_builtins
    assert frozen_builtin_items is not source_builtins


def test_detached_consumer_builtin_snapshot_has_independent_identity_anchor() -> None:
    """The builtin snapshot participates in the same independent anchor discipline."""

    guarded = run_transaction._promote_paper_book_snapshot
    require_bindings = _closure_value(guarded, "require_bindings")
    frozen_builtin_items = _closure_value(guarded, "frozen_builtin_items")

    assert isinstance(require_bindings, FunctionType)
    assert type(frozen_builtin_items) is tuple
    assert any(item is frozen_builtin_items for item in require_bindings.__code__.co_consts)
