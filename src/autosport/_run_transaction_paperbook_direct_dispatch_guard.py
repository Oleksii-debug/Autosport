"""Detach RunTransaction PaperBook persistence consumers from live module globals.

The owning PaperBook persistence graph is already sealed and RunTransaction stage/
promotion already resolves that authority through the current-binding consumer seal.
Those consumers nevertheless retained the live ``run_transaction`` globals mapping for
non-authority direct dispatch such as ``os.replace``, ``tempfile.mkstemp``,
``hashlib.sha256`` and ``PaperBook.load_bytes``.  A later module-alias/member retarget
could therefore change the executable path around the sealed persistence facade.

This module adds no persistence authority.  It detaches the already-installed guarded
consumers from that live globals mapping and gives promotion minimal immutable surfaces
for the direct stdlib/semantic call targets it already uses.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from types import FunctionType

from . import paper as _paper
from . import run_transaction as _run_transaction


class _FrozenSurface(tuple):
    __slots__ = ()

    def __new__(cls, **values: object):
        return tuple.__new__(cls, tuple(values.items()))

    def __getattr__(self, name: str) -> object:
        for member_name, member_value in self:
            if member_name == name:
                return member_value
        raise AttributeError(name)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("RunTransaction direct dispatch surface is frozen")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("RunTransaction direct dispatch surface is frozen")


def _fresh_cell(value: object):
    def capture():
        return value

    closure = capture.__closure__
    if closure is None:
        raise RuntimeError("RunTransaction dispatch closure capture failed")
    return closure[0]


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
    if function.__qualname__ == "RunTransaction._promote_paper_book_snapshot":
        if inner_globals.get("os") is not os:
            raise RuntimeError("RunTransaction canonical OS dispatch changed before sealing")
        if inner_globals.get("tempfile") is not tempfile:
            raise RuntimeError("RunTransaction canonical tempfile dispatch changed before sealing")
        if inner_globals.get("hashlib") is not hashlib:
            raise RuntimeError("RunTransaction canonical digest dispatch changed before sealing")
        if inner_globals.get("PaperBook") is not _paper.PaperBook:
            raise RuntimeError("RunTransaction canonical PaperBook dispatch changed before sealing")

        detached_globals["os"] = _FrozenSurface(
            close=os.close,
            fsync=os.fsync,
            replace=os.replace,
            name=os.name,
        )
        detached_globals["tempfile"] = _FrozenSurface(mkstemp=tempfile.mkstemp)
        detached_globals["hashlib"] = _FrozenSurface(sha256=hashlib.sha256)
        detached_globals["PaperBook"] = _FrozenSurface(
            load_bytes=_paper.PaperBook.load_bytes,
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
