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


class _FrozenSurfaceMetaMeta(type):
    """Protect the descriptor-bearing facade metaclass from one-step root removal."""


class _FrozenSurfaceMeta(type, metaclass=_FrozenSurfaceMetaMeta):
    """Dedicated metaclass so finalized facade dispatch can be data-descriptor sealed."""


class _SealedSurfaceRoot:
    __slots__ = ("_name", "_descriptor", "_owner")

    def __init__(self, name: str, descriptor: object, owner: type) -> None:
        self._name = name
        self._descriptor = descriptor
        self._owner = owner

    def __get__(self, instance: object, owner: type | None = None):
        del owner
        if instance is None:
            return self
        namespace = type.__getattribute__(self._owner, "__dict__")
        if namespace.get(self._name) is not self._descriptor:
            raise TypeError(f"canonical PaperBook frozen-surface root changed: {self._name}")
        getter = getattr(self._descriptor, "__get__", None)
        if getter is None:
            return self._descriptor
        return getter(None, self._owner)

    def __set__(self, instance: object, value: object) -> None:
        del instance, value
        raise TypeError(f"canonical PaperBook frozen-surface root is sealed: {self._name}")

    def __delete__(self, instance: object) -> None:
        del instance
        raise TypeError(f"canonical PaperBook frozen-surface root is sealed: {self._name}")


class _FrozenSurface(tuple, metaclass=_FrozenSurfaceMeta):
    """Structurally immutable persistence-member facade.

    Keep authority-bearing bindings directly in tuple payload storage. This avoids both
    explicit ``object.__setattr__`` slot replacement and reliance on a mutable dict hidden
    behind ``MappingProxyType``. ``_values`` is only a detached diagnostic projection;
    mutating any object reachable from that projection cannot retarget this facade.

    The class-level executable roots are sealed below through metaclass data descriptors.
    The descriptor-bearing metaclass roots are themselves guarded one level higher so a
    caller cannot first remove the seal with ``type.__delattr__`` and then retarget the
    already-created facade class. Positive persistence still independently witnesses the
    executable surface before and after authority-bearing dispatch.
    """

    __slots__ = ()

    def __new__(cls, **values: object):
        return tuple.__new__(cls, tuple(values.items()))

    # Keep the primary lookup root explicit so executable-surface witnesses can prove
    # its exact descriptor/code before any frozen member is consumed. It is deliberately
    # not installed as a same-named metaclass data descriptor: doing that would replace
    # the metaclass's own tp_getattro slot rather than merely sealing an instance root.
    def __getattribute__(self, name: str) -> object:
        return object.__getattribute__(self, name)

    __iter__ = tuple.__iter__

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


def _seal_surface_type() -> None:
    surface_type = _FrozenSurface
    surface_meta = _FrozenSurfaceMeta
    if type(surface_type) is not surface_meta or surface_type.__bases__ != (tuple,):
        raise RuntimeError("canonical PaperBook frozen-surface type changed before sealing")
    if type(surface_meta) is not _FrozenSurfaceMetaMeta or surface_meta.__bases__ != (type,):
        raise RuntimeError("canonical PaperBook frozen-surface metaclass changed before sealing")

    namespace = type.__getattribute__(surface_type, "__dict__")
    # __getattribute__ is intentionally witnessed by the executable graph rather than
    # represented as a same-named metaclass descriptor; see the class-level comment.
    protected = ("__new__", "__iter__", "__getattr__", "__setattr__", "__delattr__")
    descriptors: dict[str, object] = {}
    for name in protected:
        descriptor = namespace.get(name)
        if descriptor is None:
            raise RuntimeError(f"canonical PaperBook frozen-surface root missing: {name}")
        descriptors[name] = descriptor

    meta_roots: dict[str, object] = {}
    for name, descriptor in descriptors.items():
        sealed = _SealedSurfaceRoot(name, descriptor, surface_type)
        type.__setattr__(surface_meta, name, sealed)
        meta_roots[name] = sealed

    # Explicit ``type.__delattr__(surface_meta, name)`` bypasses surface_meta's own
    # ordinary __delattr__ dispatch. Put matching data descriptors on its metaclass so
    # the exact descriptor-removal-then-retarget route is rejected before the inner
    # seal can be removed. This complements (rather than replaces) the positive-path
    # executable witness that independently rejects a changed facade graph.
    for name, descriptor in meta_roots.items():
        type.__setattr__(
            _FrozenSurfaceMetaMeta,
            name,
            _SealedSurfaceRoot(name, descriptor, surface_meta),
        )

    current = type.__getattribute__(surface_type, "__dict__")
    current_meta = type.__getattribute__(surface_meta, "__dict__")
    for name, expected in descriptors.items():
        if current.get(name) is not expected:
            raise RuntimeError(f"canonical PaperBook frozen-surface root moved: {name}")
        if current_meta.get(name) is not meta_roots[name]:
            raise RuntimeError(f"canonical PaperBook frozen-surface metaclass root moved: {name}")


_seal_surface_type()
del _seal_surface_type


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
