"""Seal provider/execution Python name resolution after product composition.

Existing provider/timeout guards pin product functions, module-global callables, class
methods and executable code. Python LOAD_GLOBAL semantics can still change when a
name that resolved through builtins (or was missing) at composition time is inserted
later into the function's globals mapping. This guard adds no provider, ledger or
reconciliation authority: it witnesses the already-composed callable graphs and
fails closed before and after dispatch if global/builtin/missing resolution changes.
"""

from __future__ import annotations

from types import FunctionType

from . import betfair_timeout_reconciliation as _timeout
from . import supervised_execution as _execution
from . import supervised_provider_evidence as _provider


_EMPTY_CELL = object()
_GLOBAL = 1
_BUILTIN = 2
_MISSING = 3


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
        builtins_mapping = globals_mapping.get("__builtins__")
        if type(globals_mapping) is not dict or type(builtins_mapping) is not dict:
            raise RuntimeError("provider execution Python name-resolution mapping is invalid")

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
            if type(value) is FunctionType and value not in seen:
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


def _sealed(root: FunctionType, *, label: str) -> FunctionType:
    graph = _capture_function_graph(root)
    root_code = root.__code__
    exact_type = type
    function_type = FunctionType
    dict_type = dict
    runtime_error = RuntimeError
    value_error = ValueError
    global_source = _GLOBAL
    builtin_source = _BUILTIN
    missing_source = _MISSING

    root_marker = "__AUTOSPORT_PROVIDER_EXECUTION_ROOT_ANCHOR__"
    code_marker = "__AUTOSPORT_PROVIDER_EXECUTION_CODE_ANCHOR__"
    graph_marker = "__AUTOSPORT_PROVIDER_EXECUTION_GRAPH_ANCHOR__"
    type_marker = "__AUTOSPORT_PROVIDER_EXECUTION_TYPE_ANCHOR__"
    function_marker = "__AUTOSPORT_PROVIDER_EXECUTION_FUNCTION_ANCHOR__"
    dict_marker = "__AUTOSPORT_PROVIDER_EXECUTION_DICT_ANCHOR__"
    runtime_error_marker = "__AUTOSPORT_PROVIDER_EXECUTION_RUNTIME_ERROR_ANCHOR__"
    value_error_marker = "__AUTOSPORT_PROVIDER_EXECUTION_VALUE_ERROR_ANCHOR__"
    global_marker = "__AUTOSPORT_PROVIDER_EXECUTION_GLOBAL_SOURCE_ANCHOR__"
    builtin_marker = "__AUTOSPORT_PROVIDER_EXECUTION_BUILTIN_SOURCE_ANCHOR__"
    missing_marker = "__AUTOSPORT_PROVIDER_EXECUTION_MISSING_SOURCE_ANCHOR__"
    label_marker = "__AUTOSPORT_PROVIDER_EXECUTION_LABEL_ANCHOR__"

    def guarded(*args, **kwargs):
        anchored_root = "__AUTOSPORT_PROVIDER_EXECUTION_ROOT_ANCHOR__"
        anchored_code = "__AUTOSPORT_PROVIDER_EXECUTION_CODE_ANCHOR__"
        anchored_graph = "__AUTOSPORT_PROVIDER_EXECUTION_GRAPH_ANCHOR__"
        anchored_type = "__AUTOSPORT_PROVIDER_EXECUTION_TYPE_ANCHOR__"
        anchored_function = "__AUTOSPORT_PROVIDER_EXECUTION_FUNCTION_ANCHOR__"
        anchored_dict = "__AUTOSPORT_PROVIDER_EXECUTION_DICT_ANCHOR__"
        anchored_runtime_error = "__AUTOSPORT_PROVIDER_EXECUTION_RUNTIME_ERROR_ANCHOR__"
        anchored_value_error = "__AUTOSPORT_PROVIDER_EXECUTION_VALUE_ERROR_ANCHOR__"
        anchored_global = "__AUTOSPORT_PROVIDER_EXECUTION_GLOBAL_SOURCE_ANCHOR__"
        anchored_builtin = "__AUTOSPORT_PROVIDER_EXECUTION_BUILTIN_SOURCE_ANCHOR__"
        anchored_missing = "__AUTOSPORT_PROVIDER_EXECUTION_MISSING_SOURCE_ANCHOR__"
        anchored_label = "__AUTOSPORT_PROVIDER_EXECUTION_LABEL_ANCHOR__"

        if (
            root is not anchored_root
            or root_code is not anchored_code
            or graph is not anchored_graph
            or exact_type is not anchored_type
            or function_type is not anchored_function
            or dict_type is not anchored_dict
            or runtime_error is not anchored_runtime_error
            or value_error is not anchored_value_error
            or global_source != anchored_global
            or builtin_source != anchored_builtin
            or missing_source != anchored_missing
            or label != anchored_label
            or anchored_type(anchored_root) is not anchored_function
            or anchored_root.__code__ is not anchored_code
        ):
            raise anchored_runtime_error("provider execution guard anchor changed")

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
                anchored_type(function) is not anchored_function
                or function.__code__ is not code
                or function.__defaults__ is not defaults
                or function.__kwdefaults__ is not kwdefaults
                or function.__closure__ is not closure
                or function.__globals__ is not globals_mapping
                or anchored_type(globals_mapping) is not anchored_dict
                or anchored_type(builtins_mapping) is not anchored_dict
                or globals_mapping.get("__builtins__") is not builtins_mapping
            ):
                raise anchored_runtime_error(f"{anchored_label} executable graph changed")
            for cell, expected in cells:
                try:
                    current = cell.cell_contents
                except anchored_value_error as exc:
                    raise anchored_runtime_error(
                        f"{anchored_label} closure authority changed"
                    ) from exc
                if current is not expected:
                    raise anchored_runtime_error(
                        f"{anchored_label} closure authority changed"
                    )
            for name, source, expected, expected_code in resolutions:
                if source == anchored_global:
                    if name not in globals_mapping or globals_mapping[name] is not expected:
                        raise anchored_runtime_error(
                            f"{anchored_label} global name resolution changed: {name}"
                        )
                elif source == anchored_builtin:
                    if (
                        name in globals_mapping
                        or name not in builtins_mapping
                        or builtins_mapping[name] is not expected
                    ):
                        raise anchored_runtime_error(
                            f"{anchored_label} builtin name resolution changed: {name}"
                        )
                elif source == anchored_missing:
                    if name in globals_mapping or name in builtins_mapping:
                        raise anchored_runtime_error(
                            f"{anchored_label} missing name resolution changed: {name}"
                        )
                else:
                    raise anchored_runtime_error(
                        f"{anchored_label} name-resolution witness changed"
                    )
                if expected_code is not None and (
                    anchored_type(expected) is not anchored_function
                    or expected.__code__ is not expected_code
                ):
                    raise anchored_runtime_error(
                        f"{anchored_label} dependency executable changed: {name}"
                    )
        try:
            return anchored_root(*args, **kwargs)
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
                    anchored_type(function) is not anchored_function
                    or function.__code__ is not code
                    or function.__defaults__ is not defaults
                    or function.__kwdefaults__ is not kwdefaults
                    or function.__closure__ is not closure
                    or function.__globals__ is not globals_mapping
                    or anchored_type(globals_mapping) is not anchored_dict
                    or anchored_type(builtins_mapping) is not anchored_dict
                    or globals_mapping.get("__builtins__") is not builtins_mapping
                ):
                    raise anchored_runtime_error(
                        f"{anchored_label} executable graph changed"
                    )
                for cell, expected in cells:
                    try:
                        current = cell.cell_contents
                    except anchored_value_error as exc:
                        raise anchored_runtime_error(
                            f"{anchored_label} closure authority changed"
                        ) from exc
                    if current is not expected:
                        raise anchored_runtime_error(
                            f"{anchored_label} closure authority changed"
                        )
                for name, source, expected, expected_code in resolutions:
                    if source == anchored_global:
                        if name not in globals_mapping or globals_mapping[name] is not expected:
                            raise anchored_runtime_error(
                                f"{anchored_label} global name resolution changed: {name}"
                            )
                    elif source == anchored_builtin:
                        if (
                            name in globals_mapping
                            or name not in builtins_mapping
                            or builtins_mapping[name] is not expected
                        ):
                            raise anchored_runtime_error(
                                f"{anchored_label} builtin name resolution changed: {name}"
                            )
                    elif source == anchored_missing:
                        if name in globals_mapping or name in builtins_mapping:
                            raise anchored_runtime_error(
                                f"{anchored_label} missing name resolution changed: {name}"
                            )
                    else:
                        raise anchored_runtime_error(
                            f"{anchored_label} name-resolution witness changed"
                        )
                    if expected_code is not None and (
                        anchored_type(expected) is not anchored_function
                        or expected.__code__ is not expected_code
                    ):
                        raise anchored_runtime_error(
                            f"{anchored_label} dependency executable changed: {name}"
                        )

    constants = guarded.__code__.co_consts
    anchors = (
        (root_marker, root),
        (code_marker, root_code),
        (graph_marker, graph),
        (type_marker, exact_type),
        (function_marker, function_type),
        (dict_marker, dict_type),
        (runtime_error_marker, runtime_error),
        (value_error_marker, value_error),
        (global_marker, global_source),
        (builtin_marker, builtin_source),
        (missing_marker, missing_source),
        (label_marker, label),
    )
    if any(sum(item == marker for item in constants) != 1 for marker, _ in anchors):
        raise RuntimeError("provider execution name-resolution anchor is ambiguous")
    guarded.__code__ = guarded.__code__.replace(
        co_consts=tuple(
            next((anchored for marker, anchored in anchors if item == marker), item)
            for item in constants
        )
    )
    guarded.__name__ = root.__name__
    guarded.__qualname__ = root.__qualname__
    guarded.__module__ = root.__module__
    guarded.__doc__ = root.__doc__
    return guarded


def _install() -> None:
    timeout_resolver = _timeout.resolve_betfair_timeout_provider_state
    provider_verifier = _provider.verify_betfair_provider_state
    provider_assertion = _provider.assert_verified_provider_evidence_authoritative
    not_found_reconciler = _execution.reconcile_provider_not_found
    if not all(
        type(value) is FunctionType
        for value in (
            timeout_resolver,
            provider_verifier,
            provider_assertion,
            not_found_reconciler,
        )
    ):
        raise RuntimeError("canonical provider execution authority callables are unavailable")

    _timeout.resolve_betfair_timeout_provider_state = _sealed(
        timeout_resolver,
        label="Betfair timeout resolver",
    )
    _provider.verify_betfair_provider_state = _sealed(
        provider_verifier,
        label="Betfair provider verifier",
    )
    _provider.assert_verified_provider_evidence_authoritative = _sealed(
        provider_assertion,
        label="verified provider evidence assertion",
    )
    _execution.reconcile_provider_not_found = _sealed(
        not_found_reconciler,
        label="provider NOT_FOUND reconciler",
    )


_install()
del _install
del _sealed
del _capture_function_graph
