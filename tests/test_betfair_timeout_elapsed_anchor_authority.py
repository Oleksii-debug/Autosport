from __future__ import annotations

from types import FunctionType

import autosport.betfair_timeout_reconciliation as timeout_authority


def _closure_value(function: FunctionType, name: str):
    closure = function.__closure__
    assert closure is not None
    freevars = function.__code__.co_freevars
    assert name in freevars
    return closure[freevars.index(name)].cell_contents


def test_elapsed_visibility_anchor_state_is_not_module_writable() -> None:
    """Ordinary imports must not seed an older monotonic timeout anchor."""

    assert not hasattr(timeout_authority, "_timeout_elapsed_visibility_anchors")
    assert not hasattr(timeout_authority, "_timeout_elapsed_visibility_lock")
    assert not hasattr(timeout_authority, "_install_timeout_elapsed_visibility_authority")

    ready = timeout_authority._timeout_elapsed_visibility_ready
    retire = timeout_authority._retire_timeout_elapsed_visibility_anchor
    assert type(ready) is FunctionType
    assert type(retire) is FunctionType

    anchors = _closure_value(ready, "anchors")
    anchor_lock = _closure_value(ready, "anchor_lock")
    assert anchors is _closure_value(retire, "anchors")
    assert anchor_lock is _closure_value(retire, "anchor_lock")
    assert type(anchors) is dict

    injected = {("attacker", "attempt"): (object(), 0)}
    timeout_authority._timeout_elapsed_visibility_anchors = injected
    try:
        assert _closure_value(ready, "anchors") is anchors
        assert _closure_value(retire, "anchors") is anchors
        assert _closure_value(ready, "anchors") is not injected
    finally:
        del timeout_authority._timeout_elapsed_visibility_anchors
