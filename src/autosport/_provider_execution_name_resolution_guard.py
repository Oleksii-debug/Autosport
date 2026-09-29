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


def _assert_graph_intact(graph: tuple[tuple[object, ...], ...], *, label: str) -> None:
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
    ) in graph:
        if (
            type(function) is not FunctionType
            or function.__code__ is not code
            or function.__defaults__ is not defaults
            or function.__kwdefaults__ is not kwdefaults
            or function.__closure__ is not closure
            or function.__globals__ is not globals_mapping
            or type(globals_mapping) is not dict
            or type(builtins_mapping) is not dict
            or globals_mapping.get("__builtins__") is not builtins_mapping
        ):
            raise RuntimeError(f"{label} executable graph changed")

        for cell, expected in cells:
            try:
                current = cell.cell_contents
            except ValueError as exc:
                raise RuntimeError(f"{label} closure authority changed") from exc
            if current is not expected:
                raise RuntimeError(f"{label} closure authority changed")

        for name, source, expected, expected_code in resolutions:
            if source == _GLOBAL:
                if name not in globals_mapping or globals_mapping[name] is not expected:
                    raise RuntimeError(f"{label} global name resolution changed: {name}")
            elif source == _BUILTIN:
                if (
                    name in globals_mapping
                    or name not in builtins_mapping
                    or builtins_mapping[name] is not expected
                ):
                    raise RuntimeError(f"{label} builtin name resolution changed: {name}")
            elif source == _MISSING:
                if name in globals_mapping or name in builtins_mapping:
                    raise RuntimeError(f"{label} missing name resolution changed: {name}")
            else:
                raise RuntimeError(f"{label} name-resolution witness changed")

            if expected_code is not None and (
                type(expected) is not FunctionType or expected.__code__ is not expected_code
            ):
                raise RuntimeError(f"{label} dependency executable changed: {name}")


def _sealed(root: FunctionType, *, label: str) -> FunctionType:
    graph = _capture_function_graph(root)

    def guarded(*args, **kwargs):
        _assert_graph_intact(graph, label=label)
        try:
            return root(*args, **kwargs)
        finally:
            _assert_graph_intact(graph, label=label)

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
del _assert_graph_intact
del _capture_function_graph
