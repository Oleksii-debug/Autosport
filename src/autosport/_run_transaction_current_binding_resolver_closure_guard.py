"""Seal RunTransaction current-binding resolver closure authority before detachment.

The canonical current-binding wrapper captures resolver FunctionType objects whose
identity and code are checked at call time. Function identity/code alone is not a
complete authority witness in Python: reachable closure cells or late Python global /
builtin name resolution can be coherently retargeted while preserving both. This guard
adds no binding or persistence registry. It wraps the already-installed resolvers before
the direct-dispatch guard snapshots them and witnesses their existing transitive
FunctionType closure and name-resolution graph.
"""

from __future__ import annotations

from types import FunctionType

from . import run_transaction as _run_transaction


_EMPTY_CELL = object()
_GLOBAL = 1
_BUILTIN = 2
_MISSING = 3


def _closure_cell(function: FunctionType, name: str):
    closure = function.__closure__
    freevars = function.__code__.co_freevars
    if closure is None or name not in freevars:
        raise RuntimeError(
            f"RunTransaction current-binding resolver closure is unavailable: {name}"
        )
    return closure[freevars.index(name)]


def _capture_closure_graph(root: FunctionType) -> tuple[tuple[object, ...], ...]:
    pending = [root]
    seen: set[FunctionType] = set()
    witnesses: list[tuple[object, ...]] = []
    while pending:
        function = pending.pop()
        if function in seen:
            continue
        seen.add(function)
        closure = function.__closure__
        cells: list[tuple[object, object]] = []
        if closure is not None:
            for cell in closure:
                try:
                    value = cell.cell_contents
                except ValueError:
                    value = _EMPTY_CELL
                cells.append((cell, value))
                if type(value) is FunctionType and value not in seen:
                    pending.append(value)

        globals_mapping = function.__globals__
        builtins_mapping = globals_mapping.get("__builtins__")
        if type(globals_mapping) is not dict or type(builtins_mapping) is not dict:
            raise RuntimeError(
                "RunTransaction resolver Python global/builtin mappings are invalid"
            )
        resolutions: list[tuple[str, int, object | None, object | None]] = []
        for name in function.__code__.co_names:
            if name in globals_mapping:
                source = _GLOBAL
                value = globals_mapping[name]
            elif name in builtins_mapping:
                source = _BUILTIN
                value = builtins_mapping[name]
            else:
                source = _MISSING
                value = None
            expected_code = value.__code__ if type(value) is FunctionType else None
            resolutions.append((name, source, value, expected_code))
            if (
                source == _GLOBAL
                and type(value) is FunctionType
                and value.__globals__ is globals_mapping
                and value not in seen
            ):
                pending.append(value)

        witnesses.append(
            (
                function,
                function.__code__,
                function.__defaults__,
                function.__kwdefaults__,
                closure,
                tuple(cells),
                globals_mapping,
                builtins_mapping,
                tuple(resolutions),
            )
        )
    return tuple(witnesses)


def _sealed_callable(original_callable: FunctionType, *, label: str) -> FunctionType:
    graph = _capture_closure_graph(original_callable)
    original_code = original_callable.__code__
    exact_type = type
    function_type = FunctionType
    dict_type = dict
    global_source = _GLOBAL
    builtin_source = _BUILTIN
    missing_source = _MISSING

    callable_marker = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_CALLABLE_ANCHOR__"
    code_marker = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_CODE_ANCHOR__"
    graph_marker = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_CLOSURE_GRAPH_ANCHOR__"
    exact_type_marker = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_EXACT_TYPE_ANCHOR__"
    function_type_marker = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_FUNCTION_TYPE_ANCHOR__"
    dict_type_marker = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_DICT_TYPE_ANCHOR__"
    global_source_marker = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_GLOBAL_SOURCE_ANCHOR__"
    builtin_source_marker = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_BUILTIN_SOURCE_ANCHOR__"
    missing_source_marker = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_MISSING_SOURCE_ANCHOR__"
    failure_type_marker = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_FAILURE_TYPE_ANCHOR__"

    def sealed_resolver(*args, **kwargs):
        # Every rejection below uses this local, code-constant-anchored exception
        # class. A late module-global ``ValueError`` shadow therefore cannot execute
        # attacker code on the verifier's fail-closed path.
        ValueError = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_FAILURE_TYPE_ANCHOR__"  # noqa: N806
        anchored_callable = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_CALLABLE_ANCHOR__"
        anchored_code = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_CODE_ANCHOR__"
        anchored_graph = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_CLOSURE_GRAPH_ANCHOR__"
        anchored_exact_type = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_EXACT_TYPE_ANCHOR__"
        anchored_function_type = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_FUNCTION_TYPE_ANCHOR__"
        anchored_dict_type = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_DICT_TYPE_ANCHOR__"
        anchored_global_source = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_GLOBAL_SOURCE_ANCHOR__"
        anchored_builtin_source = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_BUILTIN_SOURCE_ANCHOR__"
        anchored_missing_source = "__AUTOSPORT_RUN_TRANSACTION_RESOLVER_MISSING_SOURCE_ANCHOR__"
        if (
            original_callable is not anchored_callable
            or original_code is not anchored_code
            or graph is not anchored_graph
            or exact_type is not anchored_exact_type
            or function_type is not anchored_function_type
            or dict_type is not anchored_dict_type
            or global_source != anchored_global_source
            or builtin_source != anchored_builtin_source
            or missing_source != anchored_missing_source
            or anchored_exact_type(anchored_callable) is not anchored_function_type
            or anchored_callable.__code__ is not anchored_code
        ):
            raise ValueError(f"RunTransaction {label} resolver authority changed")

        for (
            function,
            code,
            defaults,
            kwdefaults,
            closure,
            cells,
            globals_mapping,
            builtins_mapping,
            resolutions,
        ) in anchored_graph:
            if (
                anchored_exact_type(function) is not anchored_function_type
                or function.__code__ is not code
                or function.__defaults__ is not defaults
                or function.__kwdefaults__ is not kwdefaults
                or function.__closure__ is not closure
                or function.__globals__ is not globals_mapping
                or anchored_exact_type(globals_mapping) is not anchored_dict_type
                or anchored_exact_type(builtins_mapping) is not anchored_dict_type
                or globals_mapping.get("__builtins__") is not builtins_mapping
            ):
                raise ValueError(f"RunTransaction {label} resolver closure authority changed")
            for cell, expected in cells:
                try:
                    current = cell.cell_contents
                except ValueError as exc:
                    raise ValueError(
                        f"RunTransaction {label} resolver closure authority changed"
                    ) from exc
                if current is not expected:
                    raise ValueError(
                        f"RunTransaction {label} resolver closure authority changed"
                    )
            for name, source, expected, expected_code in resolutions:
                if source == anchored_global_source:
                    if name not in globals_mapping or globals_mapping[name] is not expected:
                        raise ValueError(
                            f"RunTransaction {label} resolver global authority changed"
                        )
                elif source == anchored_builtin_source:
                    if (
                        name in globals_mapping
                        or name not in builtins_mapping
                        or builtins_mapping[name] is not expected
                    ):
                        raise ValueError(
                            f"RunTransaction {label} resolver builtin authority changed"
                        )
                elif source == anchored_missing_source:
                    if name in globals_mapping or name in builtins_mapping:
                        raise ValueError(
                            f"RunTransaction {label} resolver name-resolution authority changed"
                        )
                else:
                    raise ValueError(
                        f"RunTransaction {label} resolver name-resolution witness changed"
                    )
                if expected_code is not None and (
                    anchored_exact_type(expected) is not anchored_function_type
                    or expected.__code__ is not expected_code
                ):
                    raise ValueError(
                        f"RunTransaction {label} resolver dependency executable changed"
                    )
        try:
            return anchored_callable(*args, **kwargs)
        finally:
            for (
                function,
                code,
                defaults,
                kwdefaults,
                closure,
                cells,
                globals_mapping,
                builtins_mapping,
                resolutions,
            ) in anchored_graph:
                if (
                    anchored_exact_type(function) is not anchored_function_type
                    or function.__code__ is not code
                    or function.__defaults__ is not defaults
                    or function.__kwdefaults__ is not kwdefaults
                    or function.__closure__ is not closure
                    or function.__globals__ is not globals_mapping
                    or anchored_exact_type(globals_mapping) is not anchored_dict_type
                    or anchored_exact_type(builtins_mapping) is not anchored_dict_type
                    or globals_mapping.get("__builtins__") is not builtins_mapping
                ):
                    raise ValueError(
                        f"RunTransaction {label} resolver closure authority changed"
                    )
                for cell, expected in cells:
                    try:
                        current = cell.cell_contents
                    except ValueError as exc:
                        raise ValueError(
                            f"RunTransaction {label} resolver closure authority changed"
                        ) from exc
                    if current is not expected:
                        raise ValueError(
                            f"RunTransaction {label} resolver closure authority changed"
                        )
                for name, source, expected, expected_code in resolutions:
                    if source == anchored_global_source:
                        if name not in globals_mapping or globals_mapping[name] is not expected:
                            raise ValueError(
                                f"RunTransaction {label} resolver global authority changed"
                            )
                    elif source == anchored_builtin_source:
                        if (
                            name in globals_mapping
                            or name not in builtins_mapping
                            or builtins_mapping[name] is not expected
                        ):
                            raise ValueError(
                                f"RunTransaction {label} resolver builtin authority changed"
                            )
                    elif source == anchored_missing_source:
                        if name in globals_mapping or name in builtins_mapping:
                            raise ValueError(
                                f"RunTransaction {label} resolver name-resolution authority changed"
                            )
                    else:
                        raise ValueError(
                            f"RunTransaction {label} resolver name-resolution witness changed"
                        )
                    if expected_code is not None and (
                        anchored_exact_type(expected) is not anchored_function_type
                        or expected.__code__ is not expected_code
                    ):
                        raise ValueError(
                            f"RunTransaction {label} resolver dependency executable changed"
                        )

    constants = sealed_resolver.__code__.co_consts
    anchors = (
        (callable_marker, original_callable),
        (code_marker, original_code),
        (graph_marker, graph),
        (exact_type_marker, exact_type),
        (function_type_marker, function_type),
        (dict_type_marker, dict_type),
        (global_source_marker, global_source),
        (builtin_source_marker, builtin_source),
        (missing_source_marker, missing_source),
        (failure_type_marker, ValueError),
    )
    if any(sum(item == marker for item in constants) != 1 for marker, _ in anchors):
        raise RuntimeError("RunTransaction resolver closure anchor is ambiguous")
    sealed_resolver.__code__ = sealed_resolver.__code__.replace(
        co_consts=tuple(
            next((anchored for marker, anchored in anchors if item == marker), item)
            for item in constants
        )
    )
    return sealed_resolver


def _seal_method(method: FunctionType) -> None:
    resolver_cell = _closure_cell(method, "resolver")
    resolver_code_cell = _closure_cell(method, "resolver_code")
    resolver = resolver_cell.cell_contents
    resolver_code = resolver_code_cell.cell_contents
    if type(resolver) is not FunctionType or resolver.__code__ is not resolver_code:
        raise RuntimeError("RunTransaction current-binding resolver is invalid")
    sealed_resolver = _sealed_callable(resolver, label="current-binding")
    resolver_cell.cell_contents = sealed_resolver
    resolver_code_cell.cell_contents = sealed_resolver.__code__

    persistence_cell = _closure_cell(method, "persistence_resolver")
    persistence_code_cell = _closure_cell(method, "persistence_resolver_code")
    persistence = persistence_cell.cell_contents
    persistence_code = persistence_code_cell.cell_contents
    if persistence is None:
        if persistence_code is not None:
            raise RuntimeError("RunTransaction persistence resolver code exists without resolver")
        return
    if type(persistence) is not FunctionType or persistence.__code__ is not persistence_code:
        raise RuntimeError("RunTransaction persistence resolver is invalid")
    sealed_persistence = _sealed_callable(persistence, label="persistence")
    persistence_cell.cell_contents = sealed_persistence
    persistence_code_cell.cell_contents = sealed_persistence.__code__


def _install() -> None:
    owner = _run_transaction.RunTransaction
    stage = vars(owner).get("_stage_paper_book_snapshot")
    promotion = vars(owner).get("_promote_paper_book_snapshot")
    if type(stage) is not FunctionType or type(promotion) is not FunctionType:
        raise RuntimeError("canonical RunTransaction binding consumers are unavailable")
    _seal_method(stage)
    _seal_method(promotion)


_install()
del _install
# Composition mutators are one-shot setup capabilities, not runtime product API.
del _seal_method
del _sealed_callable
del _capture_closure_graph
del _closure_cell
