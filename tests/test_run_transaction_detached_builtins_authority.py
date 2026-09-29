from __future__ import annotations

from types import FunctionType

import autosport.run_transaction as run_transaction


def _closure_value(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)].cell_contents


def test_detached_consumer_does_not_execute_mutated_live_builtins_dict() -> None:
    """Reachable live builtins remain tamper evidence, never execution dispatch."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)
    clone = _closure_value(guarded, "function")
    assert isinstance(clone, FunctionType)

    builtins_map = clone.__globals__.get("__builtins__")
    assert type(builtins_map) is dict
    original_dict = builtins_map.get("dict")
    assert original_dict is dict

    hostile_calls = 0

    def hostile_dict(*args, **kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        return dict(*args, **kwargs)

    builtins_map["dict"] = hostile_dict
    try:
        try:
            guarded()
        except Exception as exc:  # noqa: BLE001 - no args should reach delegate binding.
            assert isinstance(exc, TypeError)
        else:
            raise AssertionError("guarded consumer unexpectedly returned without arguments")
        assert hostile_calls == 0, "mutated live builtins dict was used for execution"
    finally:
        builtins_map["dict"] = original_dict

    assert builtins_map.get("dict") is original_dict
