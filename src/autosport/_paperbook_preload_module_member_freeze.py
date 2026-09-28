"""Freeze stdlib member dispatch used by the PaperBook witness authority.

The owning persistence guard is cloned by ``_paperbook_preload_load_dispatch_guard``.
Cloning its Python function graph is not sufficient when those functions reach through
mutable module objects such as ``json.loads`` or ``hashlib.sha256``: rebinding a member
on the shared module object would otherwise also retarget the cloned positive path.

Install immutable, minimal facades before the graph clone is taken. The existing
witness protocol remains the only persistence authority; this module only freezes the
module-member call targets that protocol already uses.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from types import MappingProxyType

from . import _paperbook_preload_authority_guard as _guard


class _FrozenSurface(tuple):
    """Structurally immutable persistence-member facade.

    Keep authority-bearing bindings directly in tuple payload storage. This avoids both
    explicit ``object.__setattr__`` slot replacement and reliance on a mutable dict hidden
    behind ``MappingProxyType``. ``_values`` is only a detached diagnostic projection;
    mutating any object reachable from that projection cannot retarget this facade.
    """

    __slots__ = ()

    def __new__(cls, **values: object):
        return tuple.__new__(cls, tuple(values.items()))

    def __getattr__(self, name: str) -> object:
        if name == "_values":
            return MappingProxyType({member_name: member_value for member_name, member_value in self})
        # Iterate through the immutable tuple payload directly. Do not resolve a
        # module-global ``tuple`` name here: an absent global captured at witness time
        # could otherwise be injected later and retarget positive persistence dispatch
        # without changing this method's code object.
        for member_name, member_value in self:
            if member_name == name:
                return member_value
        raise AttributeError(name)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("PaperBook persistence module surface is frozen")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("PaperBook persistence module surface is frozen")


def _install() -> None:
    # Refuse to bless a pre-retargeted owning graph. This composition runs
    # immediately after the guard import and before its positive graph is cloned.
    if _guard.json is not json:
        raise RuntimeError("canonical PaperBook JSON module authority changed")
    if _guard.hashlib is not hashlib:
        raise RuntimeError("canonical PaperBook digest module authority changed")
    if _guard.os is not os:
        raise RuntimeError("canonical PaperBook OS module authority changed")
    if _guard.tempfile is not tempfile:
        raise RuntimeError("canonical PaperBook tempfile module authority changed")

    frozen_path = _FrozenSurface(
        normcase=os.path.normcase,
        abspath=os.path.abspath,
    )
    _guard.json = _FrozenSurface(
        loads=json.loads,
        dumps=json.dumps,
        JSONDecodeError=json.JSONDecodeError,
    )
    _guard.hashlib = _FrozenSurface(sha256=hashlib.sha256)
    _guard.os = _FrozenSurface(
        path=frozen_path,
        fspath=os.fspath,
        fsync=os.fsync,
        close=os.close,
        name=os.name,
    )
    _guard.tempfile = _FrozenSurface(mkstemp=tempfile.mkstemp)


_install()
del _install
