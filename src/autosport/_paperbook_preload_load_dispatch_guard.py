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


def _clone_module_function_graph(function: FunctionType) -> FunctionType:
    """Freeze one external module-local function graph at its current executable state."""

    module_globals = function.__globals__
    trusted_globals: dict[str, object] = dict(module_globals)
    clones: dict[str, FunctionType] = {}
    for name, value in tuple(module_globals.items()):
        if type(value) is FunctionType and value.__globals__ is module_globals:
            clones[name] = _clone_local_function(value, trusted_globals=trusted_globals)
    trusted_globals.update(clones)
    frozen = clones.get(function.__name__)
    if frozen is None:
        raise RuntimeError("canonical independent authority-root clone is unavailable")
    return frozen


def _capture_delegate_graph(delegate: object) -> tuple[object, ...]:
    """Capture the semantic closure/global edges used by one positive-load delegate."""

    if type(delegate) is not FunctionType:
        raise RuntimeError("canonical PaperBook preload delegate is not a Python function")
    closure = delegate.__closure__
    closure_values = (
        () if closure is None else tuple(cell.cell_contents for cell in closure)
    )
    global_witnesses: list[tuple[str, object, object | None]] = []
    for name in delegate.__code__.co_names:
        if name not in delegate.__globals__:
            continue
        value = delegate.__globals__[name]
        code = value.__code__ if type(value) is FunctionType else None
        global_witnesses.append((name, value, code))
    return (
        delegate,
        delegate.__code__,
        delegate.__globals__,
        closure,
        closure_values,
        tuple(global_witnesses),
    )


def _require_delegate_graph_witnesses(witnesses: tuple[tuple[object, ...], ...]) -> None:
    """Reject retargeting before any positive parser/registry dispatch can execute."""

    for witness in witnesses:
        delegate, code, globals_mapping, closure, closure_values, globals_witnesses = witness
        if (
            type(delegate) is not FunctionType
            or delegate.__code__ is not code
            or delegate.__globals__ is not globals_mapping
            or delegate.__closure__ != closure
        ):
            raise ValueError("PaperBook positive-load delegate executable authority changed")
        current_closure = delegate.__closure__
        if current_closure is None:
            if closure_values:
                raise ValueError("PaperBook positive-load delegate closure authority changed")
        else:
            if len(current_closure) != len(closure_values):
                raise ValueError("PaperBook positive-load delegate closure authority changed")
            for cell, expected in zip(current_closure, closure_values):
                try:
                    current = cell.cell_contents
                except ValueError as exc:
                    raise ValueError(
                        "PaperBook positive-load delegate closure authority changed"
                    ) from exc
                if current is not expected:
                    raise ValueError("PaperBook positive-load delegate closure authority changed")
        for name, expected, expected_code in globals_witnesses:
            if globals_mapping.get(name) is not expected:
                raise ValueError("PaperBook positive-load delegate global authority changed")
            if expected_code is not None and (
                type(expected) is not FunctionType or expected.__code__ is not expected_code
            ):
                raise ValueError("PaperBook positive-load delegate global executable changed")


def _guarded_load_template(cls, path):
    _require_delegate_graph_witnesses(_DELEGATE_GRAPH_WITNESSES)
    result = _FROZEN_LOAD(cls, path)
    _require_delegate_graph_witnesses(_DELEGATE_GRAPH_WITNESSES)
    return result


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

    # `_paper_authority_root` is imported into the owning guard from the canonical
    # anti-rollback module. Copying that function object alone would still allow an
    # in-place `__code__` replacement to redirect witness lookup while preserving
    # identity. Freeze its module-local executable graph first, preserving the same
    # canonical root policy without creating another root selector or store.
    root_selector = guard_namespace.get("_paper_authority_root")
    if type(root_selector) is not FunctionType:
        raise RuntimeError("canonical independent PaperBook authority root changed")
    frozen_root_selector = _clone_module_function_graph(root_selector)

    # Copy the owning guard namespace once, substitute the sealed external root
    # selector, then replace every function defined by the preload guard with an
    # exact-code clone whose globals point back to this private mapping. The graph
    # remains the same implementation and state objects, but local helper dispatch
    # no longer consults caller-mutable module dictionaries/function objects.
    trusted_globals: dict[str, object] = dict(guard_namespace)
    trusted_globals["_paper_authority_root"] = frozen_root_selector
    clones: dict[str, FunctionType] = {}
    for name, value in tuple(guard_namespace.items()):
        if type(value) is FunctionType and value.__globals__ is guard_namespace:
            clones[name] = _clone_local_function(value, trusted_globals=trusted_globals)
    trusted_globals.update(clones)

    frozen_load = clones.get("_trusted_path_load")
    if frozen_load is None:
        raise RuntimeError("canonical PaperBook positive path load clone is unavailable")

    # The owning guard's older delegate witness checked only the closure *cell tuple*.
    # Cell contents remain mutable without changing tuple equality, and delegate global
    # helpers can likewise be rebound while the globals-dict identity stays constant.
    # Capture those semantic edges before the positive load is published and enforce
    # them before the canonical parser can construct/revoke/register any book authority.
    delegate_names = (
        "_LOAD_BYTES",
        "_REGISTER_OPENING",
        "_REGISTER_CAUSAL",
        "_INSTALL_OPENING",
        "_INSTALL_CAUSAL",
    )
    delegate_witnesses: list[tuple[object, ...]] = []
    for name in delegate_names:
        delegate_witnesses.append(_capture_delegate_graph(guard_namespace.get(name)))

    wrapper_globals: dict[str, object] = dict(globals())
    wrapper_globals["_DELEGATE_GRAPH_WITNESSES"] = tuple(delegate_witnesses)
    wrapper_globals["_FROZEN_LOAD"] = frozen_load
    wrapper_globals["_require_delegate_graph_witnesses"] = _clone_local_function(
        _require_delegate_graph_witnesses,
        trusted_globals=wrapper_globals,
    )
    guarded_load = _clone_local_function(
        _guarded_load_template,
        trusted_globals=wrapper_globals,
    )
    guarded_load.__name__ = "load"
    guarded_load.__qualname__ = "PaperBook.load"

    # This replaces only dispatch of the already-defined positive path-load authority.
    # Save/publication continues to use the owning preload guard unchanged, including
    # its existing crash-injection tests and PREPARE/COMMIT/ABORT witness protocol.
    paper_book.load = classmethod(guarded_load)


_install()
del _install
del _guarded_load_template
del _require_delegate_graph_witnesses
del _capture_delegate_graph
del _clone_module_function_graph
del _clone_local_function
