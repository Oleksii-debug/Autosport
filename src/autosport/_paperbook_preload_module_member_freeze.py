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
from types import FunctionType, MappingProxyType

from . import _paperbook_preload_authority_guard as _guard
from . import paper as _paper


# Filled once, before any frozen surface becomes reachable by a persistence delegate.
# Each record keeps the exact live function defaults objects plus an immutable identity
# snapshot of keyword-default entries. The final surface lookup validates the whole
# detached module-function graph, not just its public root member, so nested cloned
# helpers cannot retarget positive persistence through mutable defaults either.
_FROZEN_FUNCTION_DEFAULT_WITNESSES: tuple[
    tuple[FunctionType, object, object, tuple[tuple[str, object], ...] | None], ...
] = ()
_FROZEN_DEFAULT_MISSING = object()


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


class _SealedSurfaceMutationRoot:
    """Seal a facade mutation root without hijacking metaclass mutation dispatch."""

    __slots__ = ("_name", "_descriptor", "_owner", "_dispatcher")

    def __init__(self, name: str, descriptor: object, owner: type, dispatcher: object) -> None:
        self._name = name
        self._descriptor = descriptor
        self._owner = owner
        self._dispatcher = dispatcher

    def __get__(self, instance: object, owner: type | None = None):
        del owner
        if instance is None:
            return self
        namespace = type.__getattribute__(self._owner, "__dict__")
        if namespace.get(self._name) is not self._descriptor:
            raise TypeError(f"canonical PaperBook frozen-surface root changed: {self._name}")
        return self._dispatcher.__get__(instance, type(instance))

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
    # its exact descriptor/code before any frozen member is consumed. Resolve the
    # immutable ``object`` root through a literal tuple's C-owned type ancestry, avoiding
    # mutable globals, builtins, defaults and closure cells while keeping this code object
    # fully marshalable for py_compile/.pyc and packaged Windows builds.
    def __getattribute__(self, name: str) -> object:
        return ().__class__.__mro__[1].__getattribute__(self, name)

    __iter__ = tuple.__iter__

    def __getattr__(self, name: str) -> object:
        if name == "_values":
            return MappingProxyType({member_name: member_value for member_name, member_value in self})
        # Iterate through the immutable tuple payload directly. Do not resolve a
        # module-global ``tuple`` name here: an absent global captured at witness time
        # could otherwise be injected later and retarget positive persistence dispatch
        # without changing this method's code object.
        for member_name, member_value in self:
            if member_name != name:
                continue
            if type(member_value) is FunctionType:
                member_witnessed = False
                for (
                    function,
                    defaults,
                    kwdefaults,
                    kwdefault_items,
                ) in _FROZEN_FUNCTION_DEFAULT_WITNESSES:
                    if member_value is function:
                        member_witnessed = True
                    if (
                        function.__defaults__ is not defaults
                        or function.__kwdefaults__ is not kwdefaults
                    ):
                        raise TypeError(
                            "canonical PaperBook frozen function defaults authority changed"
                        )
                    if kwdefaults is not None:
                        if kwdefault_items is None or len(kwdefaults) != len(kwdefault_items):
                            raise TypeError(
                                "canonical PaperBook frozen function keyword defaults authority changed"
                            )
                        for key, expected in kwdefault_items:
                            if kwdefaults.get(key, _FROZEN_DEFAULT_MISSING) is not expected:
                                raise TypeError(
                                    "canonical PaperBook frozen function keyword defaults authority changed"
                                )
                if not member_witnessed:
                    raise TypeError(
                        "canonical PaperBook frozen function defaults witness is unavailable"
                    )
            return member_value
        raise AttributeError(name)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("PaperBook persistence module surface is frozen")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("PaperBook persistence module surface is frozen")


def _clone_module_function_graph(function: FunctionType, label: str) -> FunctionType:
    """Detach one stdlib Python-function graph from its live public function objects."""

    if type(function) is not FunctionType:
        raise RuntimeError(f"canonical PaperBook {label} member is not a Python function")
    module_globals = function.__globals__
    trusted_globals: dict[str, object] = dict(module_globals)
    clones: dict[str, FunctionType] = {}
    for name, value in tuple(module_globals.items()):
        if type(value) is not FunctionType or value.__globals__ is not module_globals:
            continue
        clone = FunctionType(
            value.__code__,
            trusted_globals,
            name=value.__name__,
            argdefs=value.__defaults__,
            closure=value.__closure__,
        )
        if value.__kwdefaults__ is not None:
            clone.__kwdefaults__ = dict(value.__kwdefaults__)
        clone.__annotations__ = dict(value.__annotations__)
        clone.__doc__ = value.__doc__
        clone.__qualname__ = value.__qualname__
        clones[name] = clone
    trusted_globals.update(clones)
    frozen = clones.get(function.__name__)
    if frozen is None:
        raise RuntimeError(f"canonical PaperBook {label} detached member is unavailable")
    return frozen


def _seal_surface_type() -> None:
    surface_type = _FrozenSurface
    surface_meta = _FrozenSurfaceMeta
    if type(surface_type) is not surface_meta or surface_type.__bases__ != (tuple,):
        raise RuntimeError("canonical PaperBook frozen-surface type changed before sealing")
    if type(surface_meta) is not _FrozenSurfaceMetaMeta or surface_meta.__bases__ != (type,):
        raise RuntimeError("canonical PaperBook frozen-surface metaclass changed before sealing")

    namespace = type.__getattribute__(surface_type, "__dict__")
    # __getattribute__ is intentionally witnessed by the executable graph rather than
    # represented as a same-named metaclass descriptor: doing so would replace the
    # metaclass's own tp_getattro slot rather than merely sealing an instance root.
    protected = ("__new__", "__iter__", "__getattr__", "__setattr__", "__delattr__")
    descriptors: dict[str, object] = {}
    for name in protected:
        descriptor = namespace.get(name)
        if descriptor is None:
            raise RuntimeError(f"canonical PaperBook frozen-surface root missing: {name}")
        descriptors[name] = descriptor

    meta_roots: dict[str, object] = {}
    for name, descriptor in descriptors.items():
        if name == "__setattr__":
            sealed = _SealedSurfaceMutationRoot(
                name, descriptor, surface_type, type.__setattr__
            )
        elif name == "__delattr__":
            sealed = _SealedSurfaceMutationRoot(
                name, descriptor, surface_type, type.__delattr__
            )
        else:
            sealed = _SealedSurfaceRoot(name, descriptor, surface_type)
        type.__setattr__(surface_meta, name, sealed)
        meta_roots[name] = sealed

    # Do not install the facade mutation guards as same-named roots on the
    # meta-metaclass: those names are its own mutation dispatch slots.
    for name, descriptor in meta_roots.items():
        if name in {"__setattr__", "__delattr__"}:
            continue
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
    global _FROZEN_FUNCTION_DEFAULT_WITNESSES

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
    if _paper.json is not json:
        raise RuntimeError("canonical PaperBook parser/serializer JSON module authority changed")
    if _paper.os is not os:
        raise RuntimeError("canonical PaperBook serializer OS module authority changed")
    if _paper.tempfile is not tempfile:
        raise RuntimeError("canonical PaperBook serializer tempfile module authority changed")

    # Freeze Python members on both sides of the same persistence boundary.  The
    # witness protocol itself parses/digests/creates stage files, while canonical
    # PaperBook load/save parses/serializes the snapshot.  Each Python callable gets a
    # private composition-time module-function graph; public same-object ``__code__``
    # mutation therefore cannot retarget either side through preserved object identity.
    guard_json_loads = _clone_module_function_graph(json.loads, "witness json.loads")
    guard_json_dumps = _clone_module_function_graph(json.dumps, "witness json.dumps")
    guard_mkstemp = _clone_module_function_graph(tempfile.mkstemp, "witness tempfile.mkstemp")
    guard_normcase = _clone_module_function_graph(os.path.normcase, "witness os.path.normcase")
    guard_abspath = _clone_module_function_graph(os.path.abspath, "witness os.path.abspath")
    paper_json_loads = _clone_module_function_graph(json.loads, "parser json.loads")
    paper_json_dump = _clone_module_function_graph(json.dump, "serializer json.dump")
    paper_named_temporary_file = _clone_module_function_graph(
        tempfile.NamedTemporaryFile,
        "serializer tempfile.NamedTemporaryFile",
    )

    frozen_roots = (
        guard_json_loads,
        guard_json_dumps,
        guard_mkstemp,
        guard_normcase,
        guard_abspath,
        paper_json_loads,
        paper_json_dump,
        paper_named_temporary_file,
    )
    frozen_graph_functions: list[FunctionType] = []
    seen_functions: set[FunctionType] = set()
    for root in frozen_roots:
        graph_globals = root.__globals__
        for value in graph_globals.values():
            if (
                type(value) is FunctionType
                and value.__globals__ is graph_globals
                and value not in seen_functions
            ):
                seen_functions.add(value)
                frozen_graph_functions.append(value)
    _FROZEN_FUNCTION_DEFAULT_WITNESSES = tuple(
        (
            function,
            function.__defaults__,
            function.__kwdefaults__,
            None
            if function.__kwdefaults__ is None
            else tuple(function.__kwdefaults__.items()),
        )
        for function in frozen_graph_functions
    )

    # Generation-CAS captured these callables before the module-member freeze.
    # Retarget the raw aliases to the detached executable clones used by the frozen
    # os.path facade so same-object stdlib code mutation cannot alter path identity.
    if "_LOCK_NORMCASE" in _guard.__dict__:
        _guard._LOCK_NORMCASE = guard_normcase
    if "_LOCK_ABSPATH" in _guard.__dict__:
        _guard._LOCK_ABSPATH = guard_abspath

    frozen_path = _FrozenSurface(
        normcase=guard_normcase,
        abspath=guard_abspath,
    )
    _guard.json = _FrozenSurface(
        loads=guard_json_loads,
        dumps=guard_json_dumps,
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
    _guard.tempfile = _FrozenSurface(mkstemp=guard_mkstemp)

    _paper.json = _FrozenSurface(
        loads=paper_json_loads,
        dump=paper_json_dump,
    )
    _paper.os = _FrozenSurface(
        fsync=os.fsync,
        replace=os.replace,
    )
    _paper.tempfile = _FrozenSurface(
        NamedTemporaryFile=paper_named_temporary_file,
    )

    # The original delegate witnesses were captured before composition and therefore
    # still name the live stdlib modules. Re-capture only those two canonical delegates
    # after their globals have been narrowed to the frozen detached surfaces. All later
    # load-dispatch/class-graph seals consume these exact same delegates and witnesses.
    _guard._LOAD_BYTES_WITNESS = _guard._capture_delegate_witness(
        _guard._LOAD_BYTES,
        "canonical load_bytes",
    )
    _guard._ORIGINAL_SAVE_WITNESS = _guard._capture_delegate_witness(
        _guard._ORIGINAL_SAVE,
        "canonical save",
    )


_install()
del _install
del _clone_module_function_graph
