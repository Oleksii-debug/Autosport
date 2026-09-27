"""Freeze PaperBook snapshot publication dispatch before durable authority is minted.

The owning preload authority guard remains the only PREPARE/COMMIT/ABORT protocol.
This composition step freezes that guard's local save graph and witnesses the
canonical PaperBook serializer helper graph so later module-global rebinding or
class-helper retargeting cannot alter bytes that are then published as authoritative.
"""

from __future__ import annotations

from types import FunctionType

from . import _paperbook_preload_authority_guard as _guard
from . import paper as _paper


_EMPTY_CELL = object()


def _closure_values(function: FunctionType) -> tuple[object, ...] | None:
    closure = function.__closure__
    if closure is None:
        return None
    values: list[object] = []
    for cell in closure:
        try:
            values.append(cell.cell_contents)
        except ValueError:
            values.append(_EMPTY_CELL)
    return tuple(values)


def _same_identity_values(
    current: tuple[object, ...] | None,
    expected: tuple[object, ...] | None,
) -> bool:
    if current is None or expected is None:
        return current is expected
    return len(current) == len(expected) and all(
        actual is wanted for actual, wanted in zip(current, expected)
    )


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


def _descriptor_function(descriptor: object) -> FunctionType | None:
    if type(descriptor) in {classmethod, staticmethod}:
        function = descriptor.__func__
        return function if type(function) is FunctionType else None
    if type(descriptor) is FunctionType:
        return descriptor
    return None


def _capture_callable_witness(name: str, descriptor: object, function: FunctionType) -> tuple[object, ...]:
    global_witnesses: list[
        tuple[str, object, object | None, tuple[object, ...] | None]
    ] = []
    for global_name in function.__code__.co_names:
        if global_name not in function.__globals__:
            continue
        value = function.__globals__[global_name]
        expected_code = value.__code__ if type(value) is FunctionType else None
        expected_closure = _closure_values(value) if type(value) is FunctionType else None
        global_witnesses.append((global_name, value, expected_code, expected_closure))
    return (
        name,
        descriptor,
        function,
        function.__code__,
        function.__globals__,
        _closure_values(function),
        tuple(global_witnesses),
    )


def _capture_serializer_graph(
    owner: type,
    root: FunctionType,
) -> tuple[tuple[object, ...], ...]:
    namespace = vars(owner)
    seen: set[FunctionType] = {root}
    pending: list[FunctionType] = [root]
    witnesses: list[tuple[object, ...]] = []

    while pending:
        function = pending.pop()
        for name in function.__code__.co_names:
            descriptor = namespace.get(name, _EMPTY_CELL)
            nested = _descriptor_function(descriptor)
            if nested is None or nested in seen:
                continue
            seen.add(nested)
            pending.append(nested)
            witnesses.append(_capture_callable_witness(name, descriptor, nested))
    return tuple(witnesses)


def _require_serializer_graph(
    owner: type,
    witnesses: tuple[tuple[object, ...], ...],
) -> None:
    namespace = vars(owner)
    for witness in witnesses:
        (
            name,
            descriptor,
            function,
            code,
            globals_mapping,
            closure_values,
            global_witnesses,
        ) = witness
        if namespace.get(name, _EMPTY_CELL) is not descriptor:
            raise ValueError("PaperBook save class dispatch authority changed")
        current = _descriptor_function(descriptor)
        if (
            current is not function
            or function.__code__ is not code
            or function.__globals__ is not globals_mapping
            or not _same_identity_values(_closure_values(function), closure_values)
        ):
            raise ValueError("PaperBook save class executable authority changed")
        for global_name, expected, expected_code, expected_closure in global_witnesses:
            if globals_mapping.get(global_name, _EMPTY_CELL) is not expected:
                raise ValueError("PaperBook save class global authority changed")
            if expected_code is not None:
                if type(expected) is not FunctionType or expected.__code__ is not expected_code:
                    raise ValueError("PaperBook save class global executable changed")
                if not _same_identity_values(_closure_values(expected), expected_closure):
                    raise ValueError("PaperBook save class global closure authority changed")


def _guarded_save_template(self, path) -> None:
    _require_serializer_graph(_CANONICAL_PAPER_BOOK, _SERIALIZER_GRAPH_WITNESSES)
    _FROZEN_SAVE(self, path)
    _require_serializer_graph(_CANONICAL_PAPER_BOOK, _SERIALIZER_GRAPH_WITNESSES)


def _install() -> None:
    paper_book = _paper.PaperBook
    namespace = vars(paper_book)
    current_save = namespace.get("save")
    guard_namespace = _guard.__dict__
    if current_save is not guard_namespace.get("_trusted_save"):
        raise RuntimeError("canonical PaperBook positive save authority changed")
    if type(current_save) is not FunctionType:
        raise RuntimeError("canonical PaperBook positive save must be a Python function")

    root_selector = guard_namespace.get("_paper_authority_root")
    if type(root_selector) is not FunctionType:
        raise RuntimeError("canonical independent PaperBook authority root changed")
    frozen_root_selector = _clone_module_function_graph(root_selector)

    trusted_globals: dict[str, object] = dict(guard_namespace)
    trusted_globals["_paper_authority_root"] = frozen_root_selector
    clones: dict[str, FunctionType] = {}
    for name, value in tuple(guard_namespace.items()):
        if type(value) is FunctionType and value.__globals__ is guard_namespace:
            clones[name] = _clone_local_function(value, trusted_globals=trusted_globals)
    trusted_globals.update(clones)

    frozen_save = clones.get("_trusted_save")
    if frozen_save is None:
        raise RuntimeError("canonical PaperBook positive save clone is unavailable")

    serializer = guard_namespace.get("_ORIGINAL_SAVE")
    if type(serializer) is not FunctionType:
        raise RuntimeError("canonical PaperBook serializer is unavailable")
    serializer_witnesses = _capture_serializer_graph(paper_book, serializer)

    wrapper_globals: dict[str, object] = dict(globals())
    wrapper_globals["_CANONICAL_PAPER_BOOK"] = paper_book
    wrapper_globals["_SERIALIZER_GRAPH_WITNESSES"] = serializer_witnesses
    wrapper_globals["_FROZEN_SAVE"] = frozen_save
    wrapper_globals["_require_serializer_graph"] = _clone_local_function(
        _require_serializer_graph,
        trusted_globals=wrapper_globals,
    )
    wrapper_globals["_descriptor_function"] = _clone_local_function(
        _descriptor_function,
        trusted_globals=wrapper_globals,
    )
    wrapper_globals["_closure_values"] = _clone_local_function(
        _closure_values,
        trusted_globals=wrapper_globals,
    )
    wrapper_globals["_same_identity_values"] = _clone_local_function(
        _same_identity_values,
        trusted_globals=wrapper_globals,
    )
    guarded_save = _clone_local_function(
        _guarded_save_template,
        trusted_globals=wrapper_globals,
    )
    guarded_save.__name__ = "save"
    guarded_save.__qualname__ = "PaperBook.save"
    paper_book.save = guarded_save


_install()
del _install
del _guarded_save_template
del _require_serializer_graph
del _capture_serializer_graph
del _capture_callable_witness
del _descriptor_function
del _clone_module_function_graph
del _clone_local_function
