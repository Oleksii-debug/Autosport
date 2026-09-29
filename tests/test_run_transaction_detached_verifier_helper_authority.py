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


def test_binding_verifier_rejects_dict_get_helper_retarget_before_dispatch() -> None:
    """Mutable verifier lookup helpers must not become executable authority."""

    guarded = run_transaction._promote_paper_book_snapshot
    assert isinstance(guarded, FunctionType)
    require_bindings = _closure_value(guarded, "require_bindings")
    assert isinstance(require_bindings, FunctionType)

    dict_get_cell = _closure_cell(require_bindings, "dict_get")
    original_dict_get = dict_get_cell.cell_contents
    assert original_dict_get is dict.get

    hostile_calls = 0

    def hostile_dict_get(mapping, key, default=None):
        nonlocal hostile_calls
        hostile_calls += 1
        return dict.get(mapping, key, default)

    dict_get_cell.cell_contents = hostile_dict_get
    try:
        try:
            guarded(object())
        except Exception as exc:  # noqa: BLE001 - any fail-closed rejection is acceptable.
            assert not isinstance(exc, TypeError), (
                "mutable verifier dict_get helper executed far enough to delegate "
                "argument binding instead of being rejected by identity authority"
            )
        else:
            raise AssertionError(
                "guard unexpectedly returned after verifier dict_get helper retargeting"
            )
        assert hostile_calls == 0, "hostile verifier dict_get helper was dispatched"
    finally:
        dict_get_cell.cell_contents = original_dict_get

    assert dict_get_cell.cell_contents is original_dict_get
