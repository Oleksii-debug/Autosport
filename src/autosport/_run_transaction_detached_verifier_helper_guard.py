"""Seal verifier-only lookup helpers around detached RunTransaction consumers.

The direct-dispatch guard reconstructs transaction consumers from composition-time
snapshots. Its nested binding verifier still retains Python closure cells for exact
``dict.get``/``dict.__len__`` lookup primitives. Those cells are useful tamper
evidence but must not become executable authority if retargeted after composition.

This module adds no persistence or transaction authority. It wraps only the already
installed binding verifier, code-anchors the exact original verifier and helper-cell
identities, and rejects helper drift before or after invoking the original verifier.
"""

from __future__ import annotations

from types import FunctionType

from . import run_transaction as _run_transaction


def _closure_cell(function: FunctionType, name: str):
    closure = function.__closure__
    freevars = function.__code__.co_freevars
    if closure is None or name not in freevars:
        raise RuntimeError(
            f"RunTransaction detached verifier closure is unavailable: {name}"
        )
    return closure[freevars.index(name)]


def _seal_method(method: FunctionType) -> None:
    require_cell = _closure_cell(method, "require_bindings")
    require_code_cell = _closure_cell(method, "require_bindings_code")
    original_require = require_cell.cell_contents
    original_require_code = require_code_cell.cell_contents
    if (
        type(original_require) is not FunctionType
        or original_require.__code__ is not original_require_code
    ):
        raise RuntimeError("RunTransaction detached binding verifier is invalid")

    original_dict_get_cell = _closure_cell(original_require, "dict_get")
    original_dict_len_cell = _closure_cell(original_require, "dict_len")
    if (
        original_dict_get_cell.cell_contents is not dict.get
        or original_dict_len_cell.cell_contents is not dict.__len__
    ):
        raise RuntimeError(
            "RunTransaction detached verifier lookup helpers changed before sealing"
        )

    # Keep exact helper values as named closure cells for diagnostics while injecting
    # independent expected identities into immutable code constants below.
    dict_get = dict.get
    dict_len = dict.__len__

    require_marker = "__AUTOSPORT_RUN_TRANSACTION_INNER_BINDING_VERIFIER_ANCHOR__"
    require_code_marker = "__AUTOSPORT_RUN_TRANSACTION_INNER_BINDING_CODE_ANCHOR__"
    dict_get_marker = "__AUTOSPORT_RUN_TRANSACTION_VERIFIER_DICT_GET_ANCHOR__"
    dict_len_marker = "__AUTOSPORT_RUN_TRANSACTION_VERIFIER_DICT_LEN_ANCHOR__"
    dict_get_cell_marker = "__AUTOSPORT_RUN_TRANSACTION_VERIFIER_DICT_GET_CELL_ANCHOR__"
    dict_len_cell_marker = "__AUTOSPORT_RUN_TRANSACTION_VERIFIER_DICT_LEN_CELL_ANCHOR__"

    def sealed_require_bindings() -> None:
        anchored_require = "__AUTOSPORT_RUN_TRANSACTION_INNER_BINDING_VERIFIER_ANCHOR__"
        anchored_require_code = "__AUTOSPORT_RUN_TRANSACTION_INNER_BINDING_CODE_ANCHOR__"
        anchored_dict_get = "__AUTOSPORT_RUN_TRANSACTION_VERIFIER_DICT_GET_ANCHOR__"
        anchored_dict_len = "__AUTOSPORT_RUN_TRANSACTION_VERIFIER_DICT_LEN_ANCHOR__"
        anchored_dict_get_cell = (
            "__AUTOSPORT_RUN_TRANSACTION_VERIFIER_DICT_GET_CELL_ANCHOR__"
        )
        anchored_dict_len_cell = (
            "__AUTOSPORT_RUN_TRANSACTION_VERIFIER_DICT_LEN_CELL_ANCHOR__"
        )
        if (
            original_require is not anchored_require
            or original_require_code is not anchored_require_code
            or original_require.__code__ is not anchored_require_code
            or dict_get is not anchored_dict_get
            or dict_len is not anchored_dict_len
            or original_dict_get_cell is not anchored_dict_get_cell
            or original_dict_len_cell is not anchored_dict_len_cell
            or anchored_dict_get_cell.cell_contents is not anchored_dict_get
            or anchored_dict_len_cell.cell_contents is not anchored_dict_len
        ):
            raise ValueError(
                "RunTransaction detached verifier lookup helper authority changed"
            )
        anchored_require()
        if (
            original_require is not anchored_require
            or original_require.__code__ is not anchored_require_code
            or dict_get is not anchored_dict_get
            or dict_len is not anchored_dict_len
            or original_dict_get_cell is not anchored_dict_get_cell
            or original_dict_len_cell is not anchored_dict_len_cell
            or anchored_dict_get_cell.cell_contents is not anchored_dict_get
            or anchored_dict_len_cell.cell_contents is not anchored_dict_len
        ):
            raise ValueError(
                "RunTransaction detached verifier lookup helper authority changed"
            )

    constants = sealed_require_bindings.__code__.co_consts
    anchors = (
        (require_marker, original_require),
        (require_code_marker, original_require_code),
        (dict_get_marker, dict_get),
        (dict_len_marker, dict_len),
        (dict_get_cell_marker, original_dict_get_cell),
        (dict_len_cell_marker, original_dict_len_cell),
    )
    if any(sum(item == marker for item in constants) != 1 for marker, _ in anchors):
        raise RuntimeError("RunTransaction detached verifier helper anchor is ambiguous")
    sealed_require_bindings.__code__ = sealed_require_bindings.__code__.replace(
        co_consts=tuple(
            next(
                (anchored for marker, anchored in anchors if item == marker),
                item,
            )
            for item in constants
        )
    )
    sealed_code = sealed_require_bindings.__code__

    # The outer detached consumer already code-anchors its verifier function/code
    # pair. Replace that pair atomically in closure state and in those exact code
    # constants before any later product surface can capture the method.
    method_constants = method.__code__.co_consts
    function_hits = sum(item is original_require for item in method_constants)
    code_hits = sum(item is original_require_code for item in method_constants)
    if function_hits != 1 or code_hits != 1:
        raise RuntimeError(
            "RunTransaction detached outer verifier anchors are unavailable"
        )
    method.__code__ = method.__code__.replace(
        co_consts=tuple(
            sealed_require_bindings
            if item is original_require
            else sealed_code
            if item is original_require_code
            else item
            for item in method_constants
        )
    )
    require_cell.cell_contents = sealed_require_bindings
    require_code_cell.cell_contents = sealed_code


def _install() -> None:
    owner = _run_transaction.RunTransaction
    stage = vars(owner).get("_stage_paper_book_snapshot")
    promotion = vars(owner).get("_promote_paper_book_snapshot")
    if type(stage) is not FunctionType or type(promotion) is not FunctionType:
        raise RuntimeError("canonical RunTransaction detached consumers are unavailable")
    _seal_method(stage)
    _seal_method(promotion)


_install()
del _install
