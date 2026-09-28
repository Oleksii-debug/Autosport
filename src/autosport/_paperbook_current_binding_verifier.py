"""Seal current PaperBook binding checks behind the sealed persistence graph.

No path binding, generation registry, or alternate persistence authority is created.
The installed public consumer function never stores positive binding authority in its
globals. Instead, each call resolves the verifier from the already-sealed PaperBook
load graph, witnesses that graph, reconstructs the original consumer with an ephemeral
globals snapshot, and injects the verified read-only capability only for that call.
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
            if type(value) is FunctionType and value.__globals__ is globals_mapping and value not in seen:
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


def _make_current_binding_resolver() -> FunctionType:
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
    frozen_load_code = frozen_load.__code__
    frozen_load_globals = frozen_load.__globals__

    def resolve_current_binding() -> FunctionType:
        if (
            trusted_globals.get("_FROZEN_LOAD") is not frozen_load
            or exact_type(frozen_load) is not function_type
            or frozen_load.__code__ is not frozen_load_code
            or frozen_load.__globals__ is not frozen_load_globals
            or frozen_load_globals.get("_require_bound_book") is not verifier
        ):
            raise ValueError("PaperBook current-binding persistence authority changed")
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
                        raise ValueError("PaperBook current-binding verifier closure authority changed") from exc
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
        return verifier

    return resolve_current_binding


def seal_current_binding_consumer(function: FunctionType) -> FunctionType:
    if type(function) is not FunctionType:
        raise TypeError("PaperBook current-binding consumer must be a Python function")

    resolver = _make_current_binding_resolver()
    resolver_code = resolver.__code__
    inner_code = function.__code__
    inner_name = function.__name__
    inner_qualname = function.__qualname__
    inner_doc = function.__doc__
    inner_annotations = dict(function.__annotations__)
    inner_defaults = function.__defaults__
    inner_kwdefaults = None if function.__kwdefaults__ is None else dict(function.__kwdefaults__)
    inner_closure = function.__closure__
    inner_globals = function.__globals__
    exact_type = type
    function_type = FunctionType

    def guarded_consumer(*args, **kwargs):
        if exact_type(resolver) is not function_type or resolver.__code__ is not resolver_code:
            raise ValueError("PaperBook current-binding resolver executable authority changed")
        verifier = resolver()
        call_globals = dict(inner_globals)
        call_globals["_REQUIRE_CURRENT_BINDING"] = verifier
        delegate = function_type(
            inner_code,
            call_globals,
            name=inner_name,
            argdefs=inner_defaults,
            closure=inner_closure,
        )
        if inner_kwdefaults is not None:
            delegate.__kwdefaults__ = dict(inner_kwdefaults)
        try:
            return delegate(*args, **kwargs)
        finally:
            if exact_type(resolver) is not function_type or resolver.__code__ is not resolver_code:
                raise ValueError("PaperBook current-binding resolver executable authority changed")
            resolver()

    guarded_consumer.__name__ = inner_name
    guarded_consumer.__qualname__ = inner_qualname
    guarded_consumer.__doc__ = inner_doc
    guarded_consumer.__annotations__ = inner_annotations
    return guarded_consumer
