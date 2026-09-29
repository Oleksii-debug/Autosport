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
already consumes. Promotion also keeps a closure-private exact witness of that shared
surface's executable roots, so any later class retarget fails closed even if a caller
first dismantles one or more Python metaclass sealing layers.
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
) -> FunctionType:
    """Require the frozen direct-dispatch surface immediately around promotion."""

    require_surface_authority = _make_surface_authority_checker()
    require_surface_authority_code = require_surface_authority.__code__
    exact_type = type
    function_type = FunctionType
    inner_name = function.__name__
    inner_qualname = function.__qualname__
    inner_doc = function.__doc__
    inner_annotations = dict(function.__annotations__)

    def guarded_consumer(*args, **kwargs):
        # Intentionally retain the real detached globals in this closure. Existing
        # diagnostics/tests use that exact handle to prove the consumer is detached.
        if exact_type(inner_globals) is not dict:
            raise ValueError("RunTransaction detached direct-dispatch globals changed")
        if (
            exact_type(require_surface_authority) is not function_type
            or require_surface_authority.__code__ is not require_surface_authority_code
        ):
            raise ValueError(
                "RunTransaction frozen direct-dispatch verifier executable changed"
            )
        require_surface_authority()
        try:
            return function(*args, **kwargs)
        finally:
            if (
                exact_type(require_surface_authority) is not function_type
                or require_surface_authority.__code__ is not require_surface_authority_code
            ):
                raise ValueError(
                    "RunTransaction frozen direct-dispatch verifier executable changed"
                )
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
    guard_direct_surface = False
    if function.__qualname__ == "RunTransaction._promote_paper_book_snapshot":
        if inner_globals.get("os") is not os:
            raise RuntimeError("RunTransaction canonical OS dispatch changed before sealing")
        if inner_globals.get("tempfile") is not tempfile:
            raise RuntimeError("RunTransaction canonical tempfile dispatch changed before sealing")
        if inner_globals.get("hashlib") is not hashlib:
            raise RuntimeError("RunTransaction canonical digest dispatch changed before sealing")
        if inner_globals.get("PaperBook") is not _paper.PaperBook:
            raise RuntimeError("RunTransaction canonical PaperBook dispatch changed before sealing")

        detached_globals["os"] = _FROZEN_SURFACE(
            close=os.close,
            fsync=os.fsync,
            replace=os.replace,
            name=os.name,
        )
        detached_globals["tempfile"] = _FROZEN_SURFACE(mkstemp=tempfile.mkstemp)
        detached_globals["hashlib"] = _FROZEN_SURFACE(sha256=hashlib.sha256)
        detached_globals["PaperBook"] = _FROZEN_SURFACE(
            load_bytes=_paper.PaperBook.load_bytes,
        )
        guard_direct_surface = True

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
    if guard_direct_surface:
        return _guard_surface_consumer(clone, inner_globals=detached_globals)
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
