from __future__ import annotations

from types import FunctionType

import autosport._run_transaction_detached_verifier_helper_guard as helper_guard
import autosport.run_transaction as run_transaction


def _closure_cell(function: FunctionType, name: str):
    freevars = function.__code__.co_freevars
    assert function.__closure__ is not None
    assert name in freevars
    return function.__closure__[freevars.index(name)]


def _closure_value(function: FunctionType, name: str):
    return _closure_cell(function, name).cell_contents


def _inner_binding_verifier(guarded: FunctionType) -> FunctionType:
    require_bindings = _closure_value(guarded, "require_bindings")
    assert isinstance(require_bindings, FunctionType)
    if "original_require" in require_bindings.__code__.co_freevars:
        original = _closure_value(require_bindings, "original_require")
        assert isinstance(original, FunctionType)
        return original
    return require_bindings


def _guarded_methods() -> tuple[FunctionType, FunctionType]:
    stage = run_transaction._stage_paper_book_snapshot
    promotion = run_transaction._promote_paper_book_snapshot
    assert isinstance(stage, FunctionType)
    assert isinstance(promotion, FunctionType)
    return stage, promotion


def test_binding_verifiers_reject_dict_get_helper_retarget_before_dispatch() -> None:
    """Mutable verifier lookup helpers must not become executable authority."""

    for guarded in _guarded_methods():
        original_require = _inner_binding_verifier(guarded)
        dict_get_cell = _closure_cell(original_require, "dict_get")
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


def test_binding_verifiers_reject_dict_len_helper_retarget_before_dispatch() -> None:
    """Verifier size checks must not dispatch through a retargeted helper cell."""

    for guarded in _guarded_methods():
        original_require = _inner_binding_verifier(guarded)
        dict_len_cell = _closure_cell(original_require, "dict_len")
        original_dict_len = dict_len_cell.cell_contents
        assert original_dict_len is dict.__len__

        hostile_calls = 0

        def hostile_dict_len(mapping):
            nonlocal hostile_calls
            hostile_calls += 1
            return dict.__len__(mapping)

        dict_len_cell.cell_contents = hostile_dict_len
        try:
            try:
                guarded(object())
            except Exception as exc:  # noqa: BLE001 - any fail-closed rejection is acceptable.
                assert not isinstance(exc, TypeError), (
                    "mutable verifier dict_len helper executed far enough to delegate "
                    "argument binding instead of being rejected by identity authority"
                )
            else:
                raise AssertionError(
                    "guard unexpectedly returned after verifier dict_len helper retargeting"
                )
            assert hostile_calls == 0, "hostile verifier dict_len helper was dispatched"
        finally:
            dict_len_cell.cell_contents = original_dict_len

        assert dict_len_cell.cell_contents is original_dict_len


def test_install_mutators_are_not_runtime_module_capabilities() -> None:
    """One-shot composition helpers must not remain caller-reachable after import."""

    assert not hasattr(helper_guard, "_install")
    assert not hasattr(helper_guard, "_seal_method")
    assert not hasattr(helper_guard, "_closure_cell")
