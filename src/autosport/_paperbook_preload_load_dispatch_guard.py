"""Freeze the existing PaperBook positive-load guard dispatch graph.

The owning preload authority guard remains the only witness protocol and the canonical
PaperBook parser remains the only structural parser.  This composition step snapshots
that guard's local function graph into one closure-private globals mapping before any
positive path load can be used.  Later module-global rebinding therefore cannot redirect
witness verification, recovery, authority installation, or path binding while keeping
the same public ``PaperBook.load`` surface.
"""

from __future__ import annotations

from types import FunctionType

from . import _paperbook_preload_authority_guard as _guard
from . import paper as _paper


def _clone_local_function(
    function: FunctionType,
    *,
    trusted_globals: dict[str, object],
) -> FunctionType:
    clone = FunctionType(
        function.__code__,
        trusted_globals,
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    if function.__kwdefaults__ is not None:
        clone.__kwdefaults__ = dict(function.__kwdefaults__)
    clone.__annotations__ = dict(function.__annotations__)
    clone.__doc__ = function.__doc__
    clone.__qualname__ = function.__qualname__
    return clone


def _install() -> None:
    paper_book = _paper.PaperBook
    class_namespace = vars(paper_book)
    load_descriptor = class_namespace.get("load")
    if type(load_descriptor) is not classmethod:
        raise RuntimeError("canonical PaperBook positive path load must remain a classmethod")

    original_load = load_descriptor.__func__
    guard_namespace = _guard.__dict__
    if original_load is not guard_namespace.get("_trusted_path_load"):
        raise RuntimeError("canonical PaperBook positive path load authority changed")
    if type(original_load) is not FunctionType:
        raise RuntimeError("canonical PaperBook positive path load must be a Python function")

    # Copy the module namespace once, then replace every function defined by the
    # preload guard with an exact-code clone whose globals point back to this private
    # mapping.  The graph remains the same implementation and state objects, but local
    # helper dispatch no longer consults the caller-mutable module dictionary.
    trusted_globals: dict[str, object] = dict(guard_namespace)
    clones: dict[str, FunctionType] = {}
    for name, value in tuple(guard_namespace.items()):
        if type(value) is FunctionType and value.__globals__ is guard_namespace:
            clones[name] = _clone_local_function(value, trusted_globals=trusted_globals)
    trusted_globals.update(clones)

    frozen_load = clones.get("_trusted_path_load")
    if frozen_load is None:
        raise RuntimeError("canonical PaperBook positive path load clone is unavailable")

    # This replaces only dispatch of the already-defined positive path-load authority.
    # Save/publication continues to use the owning preload guard unchanged, including
    # its existing crash-injection tests and PREPARE/COMMIT/ABORT witness protocol.
    paper_book.load = classmethod(frozen_load)


_install()
del _install
del _clone_local_function
