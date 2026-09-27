"""Read-only current PaperBook binding verification from the sealed persistence graph.

This module owns no path binding or second registry. It recovers the already-cloned
current-binding verifier from the final sealed PaperBook.load composition and wraps
that exact function graph with before/after executable and binding witnesses.
"""

from __future__ import annotations

from types import FunctionType

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


def _capture_function_graph(root: FunctionType) -> tuple[tuple[object, ...], ...]:
    pending = [root]
    seen: set[FunctionType] = set()
    witnesses: list[tuple[object, ...]] = []
    while pending:
        function = pending.pop()
        if function in seen:
            continue
        seen.add(function)
        globals_mapping = function.__globals__
        bindings: list[tuple[str, object, object | None]] = []
        for name in function.__code__.co_names:
            if name not in globals_mapping:
                continue
            value = globals_mapping[name]
            expected_code = value.__code__ if type(value) is FunctionType else None
            bindings.append((name, value, expected_code))
            if (
                type(value) is FunctionType
                and value.__globals__ is globals_mapping
                and value not in seen
            ):
                pending.append(value)
        witnesses.append(
            (
                function,
                function.__code__,
                globals_mapping,
                function.__defaults__,
                None if function.__kwdefaults__ is None else dict(function.__kwdefaults__),
                function.__closure__,
                _closure_values(function),
                tuple(bindings),
            )
        )
    return tuple(witnesses)


def _sealed_load_trusted_globals(load_wrapper: FunctionType) -> dict[str, object]:
    closure = load_wrapper.__closure__
    if closure is None:
        raise RuntimeError("canonical sealed PaperBook load wrapper has no closure authority")
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if (
            type(value) is dict
            and type(value.get("_FROZEN_LOAD")) is FunctionType
            and "_LOAD_CLASS_CALLABLE_GRAPH_WITNESSES" in value
            and type(value.get("_require_class_callable_graph_witnesses")) is FunctionType
        ):
            return value
    raise RuntimeError("canonical sealed PaperBook load trusted globals are unavailable")


def _capture_current_binding_verifier() -> FunctionType:
    descriptor = vars(_paper.PaperBook).get("load")
    if type(descriptor) is not classmethod or type(descriptor.__func__) is not FunctionType:
        raise RuntimeError("canonical sealed PaperBook load wrapper is unavailable")
    trusted_globals = _sealed_load_trusted_globals(descriptor.__func__)
    frozen_load = trusted_globals.get("_FROZEN_LOAD")
    if type(frozen_load) is not FunctionType:
        raise RuntimeError("canonical frozen PaperBook load graph is unavailable")
    verifier = frozen_load.__globals__.get("_require_bound_book")
    if type(verifier) is not FunctionType:
        raise RuntimeError("canonical sealed PaperBook current-binding verifier is unavailable")

    graph = _capture_function_graph(verifier)
    exact_type = type
    function_type = FunctionType
    empty_cell = _EMPTY_CELL

    def check_graph() -> None:
        for (
            function,
            code,
            globals_mapping,
            defaults,
            kwdefaults,
            closure,
            closure_values,
            bindings,
        ) in graph:
            if (
                exact_type(function) is not function_type
                or function.__code__ is not code
                or function.__globals__ is not globals_mapping
                or function.__defaults__ != defaults
                or function.__kwdefaults__ != kwdefaults
                or function.__closure__ != closure
            ):
                raise ValueError("PaperBook current-binding verifier executable authority changed")
            current_closure = function.__closure__
            if current_closure is None:
                if closure_values is not None:
                    raise ValueError("PaperBook current-binding verifier closure authority changed")
            else:
                if closure_values is None or len(current_closure) != len(closure_values):
                    raise ValueError("PaperBook current-binding verifier closure authority changed")
                for cell, expected in zip(current_closure, closure_values):
                    try:
                        current = cell.cell_contents
                    except ValueError as exc:
                        raise ValueError(
                            "PaperBook current-binding verifier closure authority changed"
                        ) from exc
                    if current is not expected:
                        raise ValueError("PaperBook current-binding verifier closure authority changed")
            for name, expected, expected_code in bindings:
                if globals_mapping.get(name, empty_cell) is not expected:
                    raise ValueError("PaperBook current-binding verifier global authority changed")
                if expected_code is not None and (
                    exact_type(expected) is not function_type
                    or expected.__code__ is not expected_code
                ):
                    raise ValueError("PaperBook current-binding verifier dependency executable changed")

    def require_current_binding(book: object, snapshot_path: object) -> None:
        check_graph()
        verifier(book, snapshot_path)
        check_graph()

    return require_current_binding


def bind_current_binding_verifier(function: FunctionType) -> FunctionType:
    """Clone one consumer function with the sealed verifier in detached globals."""

    if type(function) is not FunctionType:
        raise TypeError("PaperBook current-binding consumer must be a Python function")
    verifier = _capture_current_binding_verifier()
    private_globals = dict(function.__globals__)
    private_globals["_FROZEN_REQUIRE_BOUND_BOOK"] = verifier
    private_globals["_FROZEN_REQUIRE_BOUND_BOOK_CODE"] = verifier.__code__
    clone = FunctionType(
        function.__code__,
        private_globals,
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
