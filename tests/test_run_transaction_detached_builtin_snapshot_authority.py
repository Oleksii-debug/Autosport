from __future__ import annotations

from types import FunctionType

import autosport.run_transaction as run_transaction


def _closure_value(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)].cell_contents


def test_detached_consumer_does_not_share_live_builtins_mapping() -> None:
    """Detached execution must not retain the mutable source ``__builtins__`` mapping.

    ``dict(function.__globals__.items())`` only snapshots the outer globals mapping.  If
    its ``__builtins__`` value is the same mutable dictionary used by the source module,
    later builtin rebinding still retargets the supposedly detached delegate.  The
    canonical guard must therefore retain immutable builtin bindings and reconstruct a
    fresh builtin mapping for each short-lived delegate, rather than sharing this live
    dictionary by identity.
    """

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)

    detached = _closure_value(guarded, "function")
    frozen_function_globals_items = _closure_value(
        guarded,
        "frozen_function_globals_items",
    )
    assert isinstance(detached, FunctionType)
    assert type(frozen_function_globals_items) is tuple

    source_builtins = detached.__globals__.get("__builtins__")
    frozen_globals = dict(frozen_function_globals_items)
    assert "__builtins__" in frozen_globals
    assert frozen_globals["__builtins__"] is not source_builtins, (
        "detached RunTransaction delegate still shares the mutable source builtins "
        "mapping; builtin rebinding can retarget sealed execution without changing "
        "the anchored globals tuple"
    )
