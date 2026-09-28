"""Extend the PaperBook persistence witness to frozen module-surface executables.

The module-member freeze replaces mutable stdlib modules with read-only facade objects.
The facade backing mapping is immutable, but attribute lookup still dispatches through
its Python class. Extend the already-installed value-type executable witness so later
class/code substitution cannot retarget ``json.loads``, ``hashlib.sha256`` or the
other frozen persistence members before positive path load/save.
"""

from __future__ import annotations

from types import FunctionType

from . import _paperbook_preload_authority_guard as _guard
from . import paper as _paper


def _descriptor_function(descriptor: object) -> FunctionType | None:
    if type(descriptor) in {classmethod, staticmethod}:
        function = descriptor.__func__
        return function if type(function) is FunctionType else None
    if type(descriptor) is FunctionType:
        return descriptor
    return None


def _closure_values(
    function: FunctionType,
    *,
    empty_cell: object,
) -> tuple[object, ...] | None:
    closure = function.__closure__
    if closure is None:
        return None
    values: list[object] = []
    for cell in closure:
        try:
            values.append(cell.cell_contents)
        except ValueError:
            values.append(empty_cell)
    return tuple(values)


def _capture_callable_witness(
    name: str,
    descriptor: object,
    function: FunctionType,
    *,
    empty_cell: object,
) -> tuple[object, ...]:
    global_witnesses: list[
        tuple[str, object, object | None, tuple[object, ...] | None]
    ] = []
    for global_name in function.__code__.co_names:
        if global_name not in function.__globals__:
            continue
        value = function.__globals__[global_name]
        expected_code = value.__code__ if type(value) is FunctionType else None
        expected_closure = (
            _closure_values(value, empty_cell=empty_cell)
            if type(value) is FunctionType
            else None
        )
        global_witnesses.append(
            (global_name, value, expected_code, expected_closure)
        )
    return (
        name,
        descriptor,
        function,
        function.__code__,
        function.__globals__,
        _closure_values(function, empty_cell=empty_cell),
        tuple(global_witnesses),
    )


def _capture_surface_witnesses(
    surface_type: type,
    *,
    empty_cell: object,
) -> tuple[tuple[object, ...], ...]:
    witnesses: list[tuple[object, ...]] = []
    for name, descriptor in vars(surface_type).items():
        function = _descriptor_function(descriptor)
        if function is not None:
            witnesses.append(
                _capture_callable_witness(
                    name,
                    descriptor,
                    function,
                    empty_cell=empty_cell,
                )
            )
    if not witnesses:
        raise RuntimeError("PaperBook frozen persistence surface has no executable witness")
    return tuple(witnesses)


def _install() -> None:
    paper_book = _paper.PaperBook
    namespace = vars(paper_book)
    load_descriptor = namespace.get("load")
    save_descriptor = namespace.get("save")
    if type(load_descriptor) is not classmethod:
        raise RuntimeError("canonical PaperBook positive path load must remain a classmethod")
    if type(save_descriptor) is not FunctionType:
        raise RuntimeError("canonical PaperBook durable save must remain a Python function")

    load = load_descriptor.__func__
    save = save_descriptor
    if type(load) is not FunctionType:
        raise RuntimeError("canonical PaperBook positive path load must remain a Python function")
    if load.__globals__ is not save.__globals__:
        raise RuntimeError("PaperBook persistence wrappers must share one witness graph")

    wrapper_globals = load.__globals__
    existing = wrapper_globals.get("_VALUE_TYPE_CALLABLE_WITNESSES")
    empty_cell = wrapper_globals.get("_EMPTY_CELL")
    if type(existing) is not tuple or empty_cell is None:
        raise RuntimeError("PaperBook persistence value-type witness graph is unavailable")

    surfaces = (_guard.json, _guard.hashlib, _guard.os, _guard.tempfile)
    surface_type = type(surfaces[0])
    if any(type(surface) is not surface_type for surface in surfaces[1:]):
        raise RuntimeError("PaperBook frozen persistence surfaces do not share one canonical type")
    if any(owner is surface_type for owner, _witnesses in existing):
        raise RuntimeError("PaperBook frozen persistence surface type is already witnessed")

    surface_witnesses = _capture_surface_witnesses(
        surface_type,
        empty_cell=empty_cell,
    )
    wrapper_globals["_VALUE_TYPE_CALLABLE_WITNESSES"] = (
        *existing,
        (surface_type, surface_witnesses),
    )


_install()
del _install
