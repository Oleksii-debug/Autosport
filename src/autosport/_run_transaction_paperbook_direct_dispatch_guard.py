"""Detach RunTransaction PaperBook persistence consumers from live module globals.

The owning PaperBook persistence graph is already sealed and RunTransaction stage/
promotion already resolves that authority through the current-binding consumer seal.
Those consumers nevertheless retained the live ``run_transaction`` globals mapping for
non-authority direct dispatch such as ``os.replace``, ``tempfile.mkstemp``,
``hashlib.sha256`` and ``PaperBook.load_bytes``. A later module-alias/member retarget
could therefore change the executable path around the sealed persistence facade.

This module adds no persistence authority. It detaches the already-installed guarded
consumers from that live globals mapping and reuses the owning persistence graph's
already-witnessed immutable surface type for the exact direct call targets promotion
already consumes. Promotion also keeps closure-private exact witnesses of that shared
surface's executable roots and of every detached direct-dispatch binding. Execution is
reconstructed from the composition-time snapshot for every call, so the reachable
closure dict is tamper evidence only and cannot become a check/use dispatch race.
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

    def require_surface_authority() -> None:
        if type_getattribute(surface_type, "__bases__") != surface_bases:
            raise ValueError("RunTransaction frozen direct-dispatch surface type changed")
        current = type_getattribute(surface_type, "__dict__")
        for name, expected, expected_code in frozen_witnesses:
            if current.get(name) is not expected:
                raise ValueError(
                    f"RunTransaction frozen direct-dispatch surface root changed: {name}"
                )
            if expected_code is not None and (
                exact_type(expected) is not function_type
                or expected.__code__ is not expected_code
            ):
                raise ValueError(
                    f"RunTransaction frozen direct-dispatch surface executable changed: {name}"
                )

    return require_surface_authority


def _guard_surface_consumer(
    function: FunctionType,
    *,
    inner_globals: dict[str, object],
    expected_bindings: tuple[tuple[str, object], ...],
) -> FunctionType:
    """Require exact frozen surface/bindings and execute from a private snapshot."""

    require_surface_authority = _make_surface_authority_checker()
    require_surface_authority_code = require_surface_authority.__code__
    exact_type = type
    function_type = FunctionType
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
    closure_values: list[object] = []
    for cell in inner_closure:
        try:
            closure_values.append(cell.cell_contents)
        except ValueError as exc:
            raise RuntimeError("RunTransaction guarded consumer closure is empty") from exc
    if closure_values[inner_globals_index] is not inner_globals:
        raise RuntimeError("RunTransaction guarded consumer globals changed before sealing")
    frozen_closure_values = tuple_type(closure_values)
    frozen_globals_items = tuple_type(dict_items(inner_globals))
    frozen_globals_size = dict_len(inner_globals)
    missing = object()

    def require_bindings() -> None:
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

    require_bindings_code = require_bindings.__code__

    def guarded_consumer(*args, **kwargs):
        # Intentionally retain the real detached globals in this closure. Existing
        # diagnostics/tests use that exact handle to prove the consumer is detached.
        # It is never used as the execution mapping after composition.
        if (
            exact_type(require_surface_authority) is not function_type
            or require_surface_authority.__code__ is not require_surface_authority_code
        ):
            raise ValueError(
                "RunTransaction frozen direct-dispatch verifier executable changed"
            )
        if (
            exact_type(require_bindings) is not function_type
            or require_bindings.__code__ is not require_bindings_code
            or exact_type(fresh_cell) is not function_type
            or fresh_cell.__code__ is not fresh_cell_code
        ):
            raise ValueError(
                "RunTransaction detached direct-dispatch binding verifier changed"
            )
        require_bindings()
        require_surface_authority()

        call_globals = dict_type(frozen_globals_items)
        call_closure_values = list_type(frozen_closure_values)
        call_closure_values[inner_globals_index] = call_globals
        delegate = function_type(
            inner_code,
            inner_function_globals,
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
                exact_type(require_surface_authority) is not function_type
                or require_surface_authority.__code__ is not require_surface_authority_code
            ):
                raise ValueError(
                    "RunTransaction frozen direct-dispatch verifier executable changed"
                )
            if (
                exact_type(require_bindings) is not function_type
                or require_bindings.__code__ is not require_bindings_code
                or exact_type(fresh_cell) is not function_type
                or fresh_cell.__code__ is not fresh_cell_code
            ):
                raise ValueError(
                    "RunTransaction detached direct-dispatch binding verifier changed"
                )
            require_bindings()
            require_surface_authority()

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
    if expected_bindings:
        return _guard_surface_consumer(
            clone,
            inner_globals=detached_globals,
            expected_bindings=expected_bindings,
        )
    return clone


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
