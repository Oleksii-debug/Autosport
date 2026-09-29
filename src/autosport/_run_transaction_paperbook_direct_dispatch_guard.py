"""Detach RunTransaction PaperBook persistence consumers from live module globals.

The owning PaperBook persistence graph is already sealed and RunTransaction stage/
promotion already resolves that authority through the current-binding consumer seal.
Those consumers nevertheless retained live Python globals mappings around the sealed
persistence capability. A later alias/member or builtin-shadow retarget could therefore
change executable dispatch without changing the already-witnessed PaperBook graph.

This module adds no persistence authority. It detaches the already-installed guarded
consumers, reuses the owning persistence graph's witnessed immutable surface type for
promotion's exact direct call targets, and snapshots both guarded consumer closure
mappings and Python globals at composition. Each call reconstructs fresh ephemeral
mappings from those snapshots. Reachable live mappings remain tamper evidence only and
cannot become a check/use dispatch race in either transaction phase.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from types import FunctionType

from . import _paperbook_preload_module_member_freeze as _member_freeze
from . import paper as _paper
from . import run_transaction as _run_transaction


_FROZEN_SURFACE = _member_freeze._FrozenSurface
_SURFACE_ROOTS = ("__new__", "__iter__", "__getattr__", "__setattr__", "__delattr__")


def _fresh_cell(value: object):
    def capture():
        return value

    closure = capture.__closure__
    if closure is None:
        raise RuntimeError("RunTransaction dispatch closure capture failed")
    return closure[0]


def _make_surface_authority_checker() -> FunctionType:
    """Witness the exact class-dispatch roots used by every detached facade."""

    surface_type = _FROZEN_SURFACE
    exact_type = type
    function_type = FunctionType
    failure_type = ValueError
    type_getattribute = type.__getattribute__
    surface_bases = type_getattribute(surface_type, "__bases__")
    namespace = type_getattribute(surface_type, "__dict__")
    witnesses: list[tuple[str, object, object | None]] = []
    for name in _SURFACE_ROOTS:
        descriptor = namespace.get(name)
        if descriptor is None:
            raise RuntimeError(
                f"canonical RunTransaction frozen-surface root is unavailable: {name}"
            )
        code = descriptor.__code__ if exact_type(descriptor) is function_type else None
        witnesses.append((name, descriptor, code))
    frozen_witnesses = tuple(witnesses)

    surface_type_marker = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_TARGET_TYPE_ANCHOR__"
    surface_bases_marker = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_BASES_ANCHOR__"
    surface_witnesses_marker = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_WITNESSES_ANCHOR__"
    type_getattribute_marker = "__AUTOSPORT_RUN_TRANSACTION_TYPE_GETATTRIBUTE_ANCHOR__"
    exact_type_marker = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_EXACT_TYPE_ANCHOR__"
    function_type_marker = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_FUNCTION_TYPE_ANCHOR__"
    failure_type_marker = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_FAILURE_TYPE_ANCHOR__"

    def require_surface_authority() -> None:
        ValueError = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_FAILURE_TYPE_ANCHOR__"  # noqa: N806
        anchored_surface_type = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_TARGET_TYPE_ANCHOR__"
        anchored_surface_bases = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_BASES_ANCHOR__"
        anchored_frozen_witnesses = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_WITNESSES_ANCHOR__"
        anchored_type_getattribute = "__AUTOSPORT_RUN_TRANSACTION_TYPE_GETATTRIBUTE_ANCHOR__"
        anchored_exact_type = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_EXACT_TYPE_ANCHOR__"
        anchored_function_type = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_FUNCTION_TYPE_ANCHOR__"
        if (
            surface_type is not anchored_surface_type
            or surface_bases is not anchored_surface_bases
            or frozen_witnesses is not anchored_frozen_witnesses
            or type_getattribute is not anchored_type_getattribute
            or exact_type is not anchored_exact_type
            or function_type is not anchored_function_type
        ):
            raise ValueError("RunTransaction frozen direct-dispatch surface witness changed")
        if anchored_type_getattribute(anchored_surface_type, "__bases__") != anchored_surface_bases:
            raise ValueError("RunTransaction frozen direct-dispatch surface type changed")
        current = anchored_type_getattribute(anchored_surface_type, "__dict__")
        for name, expected, expected_code in anchored_frozen_witnesses:
            if current.get(name) is not expected:
                raise ValueError(
                    f"RunTransaction frozen direct-dispatch surface root changed: {name}"
                )
            if expected_code is not None and (
                anchored_exact_type(expected) is not anchored_function_type
                or expected.__code__ is not expected_code
            ):
                raise ValueError(
                    f"RunTransaction frozen direct-dispatch surface executable changed: {name}"
                )

    surface_constants = require_surface_authority.__code__.co_consts
    surface_anchors = (
        (surface_type_marker, surface_type),
        (surface_bases_marker, surface_bases),
        (surface_witnesses_marker, frozen_witnesses),
        (type_getattribute_marker, type_getattribute),
        (exact_type_marker, exact_type),
        (function_type_marker, function_type),
        (failure_type_marker, failure_type),
    )
    if any(
        sum(item == marker for item in surface_constants) != 1
        for marker, _ in surface_anchors
    ):
        raise RuntimeError("RunTransaction surface authority anchor is ambiguous")
    require_surface_authority.__code__ = require_surface_authority.__code__.replace(
        co_consts=tuple(
            next(
                (anchored for marker, anchored in surface_anchors if item == marker),
                item,
            )
            for item in surface_constants
        )
    )
    return require_surface_authority


def _guard_detached_consumer(
    function: FunctionType,
    *,
    inner_globals: dict[str, object],
    expected_bindings: tuple[tuple[str, object], ...],
    guard_surface: bool,
) -> FunctionType:
    """Execute a detached consumer only from composition-time private snapshots."""

    surface_authority = _make_surface_authority_checker() if guard_surface else None
    surface_authority_code = (
        None if surface_authority is None else surface_authority.__code__
    )
    exact_type = type
    function_type = FunctionType
    failure_type = ValueError
    dict_type = dict
    list_type = list
    tuple_type = tuple
    dict_get = dict.get
    dict_items = dict.items
    dict_len = dict.__len__
    fresh_cell = _fresh_cell
    fresh_cell_code = fresh_cell.__code__
    inner_code = function.__code__
    inner_function_globals = function.__globals__
    if exact_type(inner_function_globals) is not dict_type:
        raise RuntimeError("RunTransaction guarded consumer Python globals are invalid")
    frozen_function_globals_items = tuple_type(dict_items(inner_function_globals))
    frozen_function_globals_items_anchor = (frozen_function_globals_items,)
    live_builtins = dict_get(inner_function_globals, "__builtins__")
    if exact_type(live_builtins) is not dict_type:
        raise RuntimeError("RunTransaction guarded consumer builtins mapping is invalid")
    frozen_builtins_items = tuple_type(dict_items(live_builtins))
    frozen_builtins_items_anchor = (frozen_builtins_items,)
    inner_name = function.__name__
    inner_qualname = function.__qualname__
    inner_doc = function.__doc__
    inner_annotations = dict_type(function.__annotations__)
    inner_defaults = function.__defaults__
    inner_kwdefaults = (
        None
        if function.__kwdefaults__ is None
        else dict_type(function.__kwdefaults__)
    )
    inner_closure = function.__closure__
    inner_freevars = inner_code.co_freevars
    if inner_closure is None or "inner_globals" not in inner_freevars:
        raise RuntimeError("RunTransaction guarded consumer globals are unavailable")
    inner_globals_index = inner_freevars.index("inner_globals")
    inner_globals_cell = inner_closure[inner_globals_index]
    closure_values: list[object] = []
    for cell in inner_closure:
        try:
            closure_values.append(cell.cell_contents)
        except ValueError as exc:
            raise RuntimeError("RunTransaction guarded consumer closure is empty") from exc
    if closure_values[inner_globals_index] is not inner_globals:
        raise RuntimeError("RunTransaction guarded consumer globals changed before sealing")
    frozen_closure_values = tuple_type(closure_values)
    frozen_closure_values_anchor = (frozen_closure_values,)
    frozen_globals_items = tuple_type(dict_items(inner_globals))
    frozen_globals_items_anchor = (frozen_globals_items,)
    frozen_globals_size = dict_len(inner_globals)
    # Retain writable tuple anchors only as non-authoritative tamper evidence for focused
    # regressions. The actual composition-time roots are injected below into immutable
    # verifier code constants, so coordinated closure-cell retargeting cannot replace
    # execution snapshots, rebuild primitives, or their independent identity witnesses.
    inner_globals_anchor = (inner_globals,)
    identity_anchor_marker = "__AUTOSPORT_RUN_TRANSACTION_DETACHED_GLOBALS_IDENTITY_ANCHOR__"
    function_globals_anchor_marker = "__AUTOSPORT_RUN_TRANSACTION_FUNCTION_GLOBALS_SNAPSHOT_ANCHOR__"
    builtins_items_anchor_marker = "__AUTOSPORT_RUN_TRANSACTION_BUILTINS_SNAPSHOT_ANCHOR__"
    closure_values_anchor_marker = "__AUTOSPORT_RUN_TRANSACTION_CLOSURE_SNAPSHOT_ANCHOR__"
    globals_items_anchor_marker = "__AUTOSPORT_RUN_TRANSACTION_GLOBALS_ITEMS_SNAPSHOT_ANCHOR__"
    globals_index_anchor_marker = "__AUTOSPORT_RUN_TRANSACTION_GLOBALS_INDEX_ANCHOR__"
    globals_cell_anchor_marker = "__AUTOSPORT_RUN_TRANSACTION_GLOBALS_CELL_ANCHOR__"
    bindings_failure_type_marker = "__AUTOSPORT_RUN_TRANSACTION_BINDINGS_FAILURE_TYPE_ANCHOR__"
    missing = object()

    def require_bindings() -> None:
        ValueError = "__AUTOSPORT_RUN_TRANSACTION_BINDINGS_FAILURE_TYPE_ANCHOR__"  # noqa: N806
        anchored_inner_globals_index = "__AUTOSPORT_RUN_TRANSACTION_GLOBALS_INDEX_ANCHOR__"
        anchored_inner_globals_cell = "__AUTOSPORT_RUN_TRANSACTION_GLOBALS_CELL_ANCHOR__"
        current_closure = function.__closure__
        if (
            inner_globals_index != anchored_inner_globals_index
            or inner_globals_cell is not anchored_inner_globals_cell
            or exact_type(function) is not function_type
            or function.__code__ is not inner_code
            or current_closure is None
            or current_closure[anchored_inner_globals_index] is not anchored_inner_globals_cell
        ):
            raise ValueError("RunTransaction detached direct-dispatch executable changed")
        try:
            current_inner_globals = anchored_inner_globals_cell.cell_contents
        except ValueError as exc:
            raise ValueError(
                "RunTransaction detached direct-dispatch globals cell is empty"
            ) from exc
        anchored_inner_globals = "__AUTOSPORT_RUN_TRANSACTION_DETACHED_GLOBALS_IDENTITY_ANCHOR__"
        anchored_function_globals_items = "__AUTOSPORT_RUN_TRANSACTION_FUNCTION_GLOBALS_SNAPSHOT_ANCHOR__"
        anchored_builtins_items = "__AUTOSPORT_RUN_TRANSACTION_BUILTINS_SNAPSHOT_ANCHOR__"
        anchored_closure_values = "__AUTOSPORT_RUN_TRANSACTION_CLOSURE_SNAPSHOT_ANCHOR__"
        anchored_globals_items = "__AUTOSPORT_RUN_TRANSACTION_GLOBALS_ITEMS_SNAPSHOT_ANCHOR__"
        if exact_type(inner_globals_anchor) is not tuple_type:
            raise ValueError("RunTransaction detached direct-dispatch anchor changed")
        if (
            current_inner_globals is not anchored_inner_globals
            or inner_globals is not anchored_inner_globals
        ):
            raise ValueError(
                "RunTransaction detached direct-dispatch globals identity changed"
            )
        if frozen_function_globals_items is not anchored_function_globals_items:
            raise ValueError(
                "RunTransaction detached function-globals snapshot identity changed"
            )
        if frozen_builtins_items is not anchored_builtins_items:
            raise ValueError("RunTransaction detached builtins snapshot identity changed")
        if frozen_closure_values is not anchored_closure_values:
            raise ValueError("RunTransaction detached closure snapshot identity changed")
        if frozen_globals_items is not anchored_globals_items:
            raise ValueError("RunTransaction detached globals snapshot identity changed")
        if exact_type(inner_globals) is not dict_type:
            raise ValueError("RunTransaction detached direct-dispatch globals changed")
        if dict_len(inner_globals) != frozen_globals_size:
            raise ValueError("RunTransaction detached direct-dispatch globals changed")
        for name, expected in frozen_globals_items:
            if dict_get(inner_globals, name, missing) is not expected:
                raise ValueError(
                    f"RunTransaction detached direct-dispatch binding changed: {name}"
                )
        for name, expected in expected_bindings:
            if dict_get(inner_globals, name, missing) is not expected:
                raise ValueError(
                    f"RunTransaction detached direct-dispatch binding changed: {name}"
                )

    anchor_constants = require_bindings.__code__.co_consts
    anchor_markers = (
        identity_anchor_marker,
        function_globals_anchor_marker,
        builtins_items_anchor_marker,
        closure_values_anchor_marker,
        globals_items_anchor_marker,
        globals_index_anchor_marker,
        globals_cell_anchor_marker,
        bindings_failure_type_marker,
    )
    if any(sum(item == marker for item in anchor_constants) != 1 for marker in anchor_markers):
        raise RuntimeError("RunTransaction detached snapshot identity anchor is ambiguous")
    require_bindings.__code__ = require_bindings.__code__.replace(
        co_consts=tuple_type(
            inner_globals
            if item == identity_anchor_marker
            else frozen_function_globals_items
            if item == function_globals_anchor_marker
            else frozen_builtins_items
            if item == builtins_items_anchor_marker
            else frozen_closure_values
            if item == closure_values_anchor_marker
            else frozen_globals_items
            if item == globals_items_anchor_marker
            else inner_globals_index
            if item == globals_index_anchor_marker
            else inner_globals_cell
            if item == globals_cell_anchor_marker
            else failure_type
            if item == bindings_failure_type_marker
            else item
            for item in anchor_constants
        )
    )
    require_bindings_code = require_bindings.__code__

    direct_surface_authority_marker = "__AUTOSPORT_RUN_TRANSACTION_DIRECT_SURFACE_AUTHORITY_ANCHOR__"
    direct_surface_authority_code_marker = "__AUTOSPORT_RUN_TRANSACTION_DIRECT_SURFACE_AUTHORITY_CODE_ANCHOR__"
    direct_surface_failure_type_marker = "__AUTOSPORT_RUN_TRANSACTION_DIRECT_SURFACE_FAILURE_TYPE_ANCHOR__"

    def require_surface() -> None:
        ValueError = "__AUTOSPORT_RUN_TRANSACTION_DIRECT_SURFACE_FAILURE_TYPE_ANCHOR__"  # noqa: N806
        anchored_surface_authority = "__AUTOSPORT_RUN_TRANSACTION_DIRECT_SURFACE_AUTHORITY_ANCHOR__"
        anchored_surface_authority_code = "__AUTOSPORT_RUN_TRANSACTION_DIRECT_SURFACE_AUTHORITY_CODE_ANCHOR__"
        if surface_authority is None:
            return
        if (
            surface_authority is not anchored_surface_authority
            or surface_authority_code is not anchored_surface_authority_code
            or exact_type(surface_authority) is not function_type
            or surface_authority.__code__ is not anchored_surface_authority_code
            or surface_authority.__code__ is not surface_authority_code
        ):
            raise ValueError(
                "RunTransaction frozen direct-dispatch verifier executable changed"
            )
        surface_authority()

    require_surface_constants = require_surface.__code__.co_consts
    direct_surface_anchors = (
        (direct_surface_authority_marker, surface_authority),
        (direct_surface_authority_code_marker, surface_authority_code),
        (direct_surface_failure_type_marker, failure_type),
    )
    if any(
        sum(item == marker for item in require_surface_constants) != 1
        for marker, _ in direct_surface_anchors
    ):
        raise RuntimeError("RunTransaction direct surface verifier anchor is ambiguous")
    require_surface.__code__ = require_surface.__code__.replace(
        co_consts=tuple_type(
            next(
                (anchored for marker, anchored in direct_surface_anchors if item == marker),
                item,
            )
            for item in require_surface_constants
        )
    )
    require_surface_code = require_surface.__code__
    binding_function_marker = "__AUTOSPORT_RUN_TRANSACTION_BINDING_VERIFIER_FUNCTION_ANCHOR__"
    binding_code_marker = "__AUTOSPORT_RUN_TRANSACTION_BINDING_VERIFIER_CODE_ANCHOR__"
    surface_function_marker = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_VERIFIER_FUNCTION_ANCHOR__"
    surface_code_marker = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_VERIFIER_CODE_ANCHOR__"
    fresh_cell_function_marker = "__AUTOSPORT_RUN_TRANSACTION_FRESH_CELL_FUNCTION_ANCHOR__"
    fresh_cell_code_marker = "__AUTOSPORT_RUN_TRANSACTION_FRESH_CELL_CODE_ANCHOR__"
    exact_type_marker = "__AUTOSPORT_RUN_TRANSACTION_EXACT_TYPE_ANCHOR__"
    function_type_marker = "__AUTOSPORT_RUN_TRANSACTION_FUNCTION_TYPE_ANCHOR__"
    dict_type_marker = "__AUTOSPORT_RUN_TRANSACTION_DICT_TYPE_ANCHOR__"
    list_type_marker = "__AUTOSPORT_RUN_TRANSACTION_LIST_TYPE_ANCHOR__"
    tuple_type_marker = "__AUTOSPORT_RUN_TRANSACTION_TUPLE_TYPE_ANCHOR__"
    builtins_snapshot_marker = "__AUTOSPORT_RUN_TRANSACTION_PRIVATE_BUILTINS_ANCHOR__"
    function_marker = "__AUTOSPORT_RUN_TRANSACTION_DETACHED_FUNCTION_ANCHOR__"
    inner_code_marker = "__AUTOSPORT_RUN_TRANSACTION_DETACHED_CODE_ANCHOR__"
    surface_authority_marker = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_AUTHORITY_ANCHOR__"
    surface_authority_code_marker = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_AUTHORITY_CODE_ANCHOR__"
    guarded_failure_type_marker = "__AUTOSPORT_RUN_TRANSACTION_GUARDED_FAILURE_TYPE_ANCHOR__"

    def guarded_consumer(*args, **kwargs):
        # Intentionally retain the real detached globals in this closure. Existing
        # diagnostics/tests use that exact handle to prove the consumer is detached.
        # Neither it nor the owning guard module globals are execution mappings now.
        ValueError = "__AUTOSPORT_RUN_TRANSACTION_GUARDED_FAILURE_TYPE_ANCHOR__"  # noqa: N806
        anchored_require_bindings = "__AUTOSPORT_RUN_TRANSACTION_BINDING_VERIFIER_FUNCTION_ANCHOR__"
        anchored_require_bindings_code = "__AUTOSPORT_RUN_TRANSACTION_BINDING_VERIFIER_CODE_ANCHOR__"
        anchored_require_surface = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_VERIFIER_FUNCTION_ANCHOR__"
        anchored_require_surface_code = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_VERIFIER_CODE_ANCHOR__"
        anchored_fresh_cell = "__AUTOSPORT_RUN_TRANSACTION_FRESH_CELL_FUNCTION_ANCHOR__"
        anchored_fresh_cell_code = "__AUTOSPORT_RUN_TRANSACTION_FRESH_CELL_CODE_ANCHOR__"
        anchored_exact_type = "__AUTOSPORT_RUN_TRANSACTION_EXACT_TYPE_ANCHOR__"
        anchored_function_type = "__AUTOSPORT_RUN_TRANSACTION_FUNCTION_TYPE_ANCHOR__"
        anchored_dict_type = "__AUTOSPORT_RUN_TRANSACTION_DICT_TYPE_ANCHOR__"
        anchored_list_type = "__AUTOSPORT_RUN_TRANSACTION_LIST_TYPE_ANCHOR__"
        anchored_tuple_type = "__AUTOSPORT_RUN_TRANSACTION_TUPLE_TYPE_ANCHOR__"
        anchored_builtins_items = "__AUTOSPORT_RUN_TRANSACTION_PRIVATE_BUILTINS_ANCHOR__"
        anchored_function = "__AUTOSPORT_RUN_TRANSACTION_DETACHED_FUNCTION_ANCHOR__"
        anchored_inner_code = "__AUTOSPORT_RUN_TRANSACTION_DETACHED_CODE_ANCHOR__"
        anchored_surface_authority = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_AUTHORITY_ANCHOR__"
        anchored_surface_authority_code = "__AUTOSPORT_RUN_TRANSACTION_SURFACE_AUTHORITY_CODE_ANCHOR__"
        anchored_inner_globals_index = "__AUTOSPORT_RUN_TRANSACTION_GLOBALS_INDEX_ANCHOR__"
        anchored_inner_globals_cell = "__AUTOSPORT_RUN_TRANSACTION_GLOBALS_CELL_ANCHOR__"
        if (
            inner_globals_index != anchored_inner_globals_index
            or inner_globals_cell is not anchored_inner_globals_cell
            or exact_type is not anchored_exact_type
            or function_type is not anchored_function_type
            or dict_type is not anchored_dict_type
            or list_type is not anchored_list_type
            or tuple_type is not anchored_tuple_type
            or frozen_builtins_items is not anchored_builtins_items
            or function is not anchored_function
            or inner_code is not anchored_inner_code
            or surface_authority is not anchored_surface_authority
            or surface_authority_code is not anchored_surface_authority_code
            or exact_type(function) is not function_type
            or function.__code__ is not anchored_inner_code
            or function.__code__ is not inner_code
            or function.__closure__ is not inner_closure
            or frozen_function_globals_items is not frozen_function_globals_items_anchor[0]
            or frozen_builtins_items is not frozen_builtins_items_anchor[0]
            or frozen_closure_values is not frozen_closure_values_anchor[0]
            or frozen_globals_items is not frozen_globals_items_anchor[0]
            or require_surface is not anchored_require_surface
            or exact_type(require_surface) is not function_type
            or require_surface.__code__ is not anchored_require_surface_code
            or require_surface.__code__ is not require_surface_code
            or require_bindings is not anchored_require_bindings
            or exact_type(require_bindings) is not function_type
            or require_bindings.__code__ is not anchored_require_bindings_code
            or require_bindings.__code__ is not require_bindings_code
            or fresh_cell is not anchored_fresh_cell
            or exact_type(fresh_cell) is not function_type
            or fresh_cell.__code__ is not anchored_fresh_cell_code
            or fresh_cell.__code__ is not fresh_cell_code
        ):
            raise ValueError(
                "RunTransaction detached direct-dispatch verifier changed"
            )
        require_bindings()
        require_surface()

        call_function_globals = dict_type(frozen_function_globals_items)
        call_function_globals["__builtins__"] = dict_type(frozen_builtins_items)
        call_globals = dict_type(frozen_globals_items)
        call_closure_values = list_type(frozen_closure_values)
        call_closure_values[anchored_inner_globals_index] = call_globals
        delegate = function_type(
            inner_code,
            call_function_globals,
            name=inner_name,
            argdefs=inner_defaults,
            closure=tuple_type(fresh_cell(value) for value in call_closure_values),
        )
        if inner_kwdefaults is not None:
            delegate.__kwdefaults__ = dict_type(inner_kwdefaults)
        try:
            return delegate(*args, **kwargs)
        finally:
            if (
                inner_globals_index != anchored_inner_globals_index
                or inner_globals_cell is not anchored_inner_globals_cell
                or exact_type is not anchored_exact_type
                or function_type is not anchored_function_type
                or dict_type is not anchored_dict_type
                or list_type is not anchored_list_type
                or tuple_type is not anchored_tuple_type
                or frozen_builtins_items is not anchored_builtins_items
                or function is not anchored_function
                or inner_code is not anchored_inner_code
                or surface_authority is not anchored_surface_authority
                or surface_authority_code is not anchored_surface_authority_code
                or exact_type(function) is not function_type
                or function.__code__ is not anchored_inner_code
                or function.__code__ is not inner_code
                or function.__closure__ is not inner_closure
                or frozen_function_globals_items is not frozen_function_globals_items_anchor[0]
                or frozen_builtins_items is not frozen_builtins_items_anchor[0]
                or frozen_closure_values is not frozen_closure_values_anchor[0]
                or frozen_globals_items is not frozen_globals_items_anchor[0]
                or require_surface is not anchored_require_surface
                or exact_type(require_surface) is not function_type
                or require_surface.__code__ is not anchored_require_surface_code
                or require_surface.__code__ is not require_surface_code
                or require_bindings is not anchored_require_bindings
                or exact_type(require_bindings) is not function_type
                or require_bindings.__code__ is not anchored_require_bindings_code
                or require_bindings.__code__ is not require_bindings_code
                or fresh_cell is not anchored_fresh_cell
                or exact_type(fresh_cell) is not function_type
                or fresh_cell.__code__ is not anchored_fresh_cell_code
                or fresh_cell.__code__ is not fresh_cell_code
            ):
                raise ValueError(
                    "RunTransaction detached direct-dispatch verifier changed"
                )
            require_bindings()
            require_surface()

    guarded_constants = guarded_consumer.__code__.co_consts
    guarded_anchors = (
        (binding_function_marker, require_bindings),
        (binding_code_marker, require_bindings_code),
        (surface_function_marker, require_surface),
        (surface_code_marker, require_surface_code),
        (fresh_cell_function_marker, fresh_cell),
        (fresh_cell_code_marker, fresh_cell_code),
        (exact_type_marker, exact_type),
        (function_type_marker, function_type),
        (dict_type_marker, dict_type),
        (list_type_marker, list_type),
        (tuple_type_marker, tuple_type),
        (builtins_snapshot_marker, frozen_builtins_items),
        (function_marker, function),
        (inner_code_marker, inner_code),
        (surface_authority_marker, surface_authority),
        (surface_authority_code_marker, surface_authority_code),
        (globals_index_anchor_marker, inner_globals_index),
        (globals_cell_anchor_marker, inner_globals_cell),
        (guarded_failure_type_marker, failure_type),
    )
    if any(sum(item == marker for item in guarded_constants) != 1 for marker, _ in guarded_anchors):
        raise RuntimeError("RunTransaction detached verifier identity anchor is ambiguous")
    guarded_consumer.__code__ = guarded_consumer.__code__.replace(
        co_consts=tuple_type(
            next(
                (anchored for marker, anchored in guarded_anchors if item == marker),
                item,
            )
            for item in guarded_constants
        )
    )

    guarded_consumer.__name__ = inner_name
    guarded_consumer.__qualname__ = inner_qualname
    guarded_consumer.__doc__ = inner_doc
    guarded_consumer.__annotations__ = inner_annotations
    return guarded_consumer


def _detach_consumer(function: FunctionType) -> FunctionType:
    closure = function.__closure__
    freevars = function.__code__.co_freevars
    if closure is None or "inner_globals" not in freevars:
        raise RuntimeError("RunTransaction sealed consumer globals are unavailable")

    globals_index = freevars.index("inner_globals")
    try:
        inner_globals = closure[globals_index].cell_contents
    except ValueError as exc:
        raise RuntimeError("RunTransaction sealed consumer globals are empty") from exc
    if type(inner_globals) is not dict:
        raise RuntimeError("RunTransaction sealed consumer globals are invalid")

    detached_globals = dict(inner_globals)
    expected_bindings: tuple[tuple[str, object], ...] = ()
    guard_surface = False
    if function.__qualname__ == "RunTransaction._promote_paper_book_snapshot":
        if inner_globals.get("os") is not os:
            raise RuntimeError("RunTransaction canonical OS dispatch changed before sealing")
        if inner_globals.get("tempfile") is not tempfile:
            raise RuntimeError("RunTransaction canonical tempfile dispatch changed before sealing")
        if inner_globals.get("hashlib") is not hashlib:
            raise RuntimeError("RunTransaction canonical digest dispatch changed before sealing")
        if inner_globals.get("PaperBook") is not _paper.PaperBook:
            raise RuntimeError("RunTransaction canonical PaperBook dispatch changed before sealing")

        frozen_os = _FROZEN_SURFACE(
            close=os.close,
            fsync=os.fsync,
            replace=os.replace,
            name=os.name,
        )
        frozen_tempfile = _FROZEN_SURFACE(mkstemp=tempfile.mkstemp)
        frozen_hashlib = _FROZEN_SURFACE(sha256=hashlib.sha256)
        frozen_paper_book = _FROZEN_SURFACE(
            load_bytes=_paper.PaperBook.load_bytes,
        )
        detached_globals["os"] = frozen_os
        detached_globals["tempfile"] = frozen_tempfile
        detached_globals["hashlib"] = frozen_hashlib
        detached_globals["PaperBook"] = frozen_paper_book
        expected_bindings = (
            ("os", frozen_os),
            ("tempfile", frozen_tempfile),
            ("hashlib", frozen_hashlib),
            ("PaperBook", frozen_paper_book),
        )
        guard_surface = True

    detached_closure = list(closure)
    detached_closure[globals_index] = _fresh_cell(detached_globals)
    clone = FunctionType(
        function.__code__,
        function.__globals__,
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=tuple(detached_closure),
    )
    if function.__kwdefaults__ is not None:
        clone.__kwdefaults__ = dict(function.__kwdefaults__)
    clone.__qualname__ = function.__qualname__
    clone.__doc__ = function.__doc__
    clone.__annotations__ = dict(function.__annotations__)
    return _guard_detached_consumer(
        clone,
        inner_globals=detached_globals,
        expected_bindings=expected_bindings,
        guard_surface=guard_surface,
    )


def _install() -> None:
    owner = _run_transaction.RunTransaction
    stage = vars(owner).get("_stage_paper_book_snapshot")
    promotion = vars(owner).get("_promote_paper_book_snapshot")
    if type(stage) is not FunctionType or type(promotion) is not FunctionType:
        raise RuntimeError("canonical RunTransaction PaperBook consumers are unavailable")

    owner._stage_paper_book_snapshot = _detach_consumer(stage)
    owner._promote_paper_book_snapshot = _detach_consumer(promotion)


_install()
del _install
