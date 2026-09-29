"""Seal the positive campaign-precommit publisher dispatch graph after composition.

The owning :mod:`campaign_precommit_manifest` module remains the sole manifest and
publication-witness authority.  This guard does not copy its persistence protocol or
create another trust root.  It snapshots the already-built positive publisher's
ordinary Python executable/global/closure graph and the exact monotonic-authority
methods it dispatches through, refusing to dispatch if either surface is later
retargeted through ordinary Python rebinding.

This is a TRUSTED_PRODUCT_INTERPRETER composition fence, not a sandbox against code
that already controls arbitrary closure cells, code objects, or interpreter internals.
"""

from __future__ import annotations

from types import FunctionType

from . import campaign_precommit_manifest as _precommit


_EMPTY = object()
_FUNCTION_TYPE = FunctionType
_AUTHORITY_METHOD_NAMES = (
    "__init__",
    "abort",
    "commit",
    "prepare",
    "read_history",
    "recover",
)


def _closure_values(function: FunctionType) -> tuple[object, ...] | None:
    closure = function.__closure__
    if closure is None:
        return None
    values: list[object] = []
    for cell in closure:
        try:
            values.append(cell.cell_contents)
        except ValueError:
            values.append(_EMPTY)
    return tuple(values)


def _kwdefault_items(function: FunctionType) -> tuple[tuple[str, object], ...] | None:
    kwdefaults = function.__kwdefaults__
    if kwdefaults is None:
        return None
    return tuple(kwdefaults.items())


def _capture_function_graph(root: object, label: str) -> tuple[tuple[object, ...], ...]:
    if type(root) is not _FUNCTION_TYPE:
        raise RuntimeError(f"canonical {label} is not a Python function")

    pending: list[FunctionType] = [root]
    seen: set[FunctionType] = set()
    nodes: list[tuple[object, ...]] = []
    while pending:
        function = pending.pop()
        if function in seen:
            continue
        seen.add(function)

        globals_mapping = function.__globals__
        global_bindings: list[tuple[str, object, object | None]] = []
        for name in function.__code__.co_names:
            if name not in globals_mapping:
                continue
            expected = globals_mapping[name]
            expected_code = (
                expected.__code__ if type(expected) is _FUNCTION_TYPE else None
            )
            global_bindings.append((name, expected, expected_code))
            if type(expected) is _FUNCTION_TYPE:
                pending.append(expected)

        closure = function.__closure__
        closure_values = _closure_values(function)
        if closure_values is not None:
            for value in closure_values:
                if type(value) is _FUNCTION_TYPE:
                    pending.append(value)

        nodes.append(
            (
                function,
                function.__code__,
                globals_mapping,
                function.__defaults__,
                function.__kwdefaults__,
                _kwdefault_items(function),
                closure,
                closure_values,
                tuple(global_bindings),
            )
        )
    return tuple(nodes)


def _require_function_graph(
    graph: tuple[tuple[object, ...], ...],
    label: str,
) -> None:
    for node in graph:
        (
            function,
            expected_code,
            globals_mapping,
            expected_defaults,
            expected_kwdefaults,
            expected_kwdefault_items,
            expected_closure,
            expected_closure_values,
            global_bindings,
        ) = node
        if (
            type(function) is not _FUNCTION_TYPE
            or function.__code__ is not expected_code
            or function.__globals__ is not globals_mapping
            or function.__defaults__ is not expected_defaults
            or function.__kwdefaults__ is not expected_kwdefaults
            or function.__closure__ is not expected_closure
        ):
            raise RuntimeError(f"{label} executable authority changed after composition")

        if expected_kwdefaults is not None:
            if (
                expected_kwdefault_items is None
                or len(expected_kwdefaults) != len(expected_kwdefault_items)
            ):
                raise RuntimeError(
                    f"{label} keyword-default authority changed after composition"
                )
            for key, expected in expected_kwdefault_items:
                if expected_kwdefaults.get(key, _EMPTY) is not expected:
                    raise RuntimeError(
                        f"{label} keyword-default authority changed after composition"
                    )

        current_closure_values = _closure_values(function)
        if expected_closure_values is None:
            if current_closure_values is not None:
                raise RuntimeError(f"{label} closure authority changed after composition")
        elif (
            current_closure_values is None
            or len(current_closure_values) != len(expected_closure_values)
            or any(
                current is not expected
                for current, expected in zip(
                    current_closure_values,
                    expected_closure_values,
                )
            )
        ):
            raise RuntimeError(f"{label} closure authority changed after composition")

        for name, expected, expected_global_code in global_bindings:
            if globals_mapping.get(name, _EMPTY) is not expected:
                raise RuntimeError(f"{label} global dispatch authority changed: {name}")
            if expected_global_code is not None and (
                type(expected) is not _FUNCTION_TYPE
                or expected.__code__ is not expected_global_code
            ):
                raise RuntimeError(
                    f"{label} global executable authority changed: {name}"
                )


def _capture_class_method_graph(
    owner: object,
    method_names: tuple[str, ...],
    label: str,
) -> tuple[tuple[object, str, FunctionType, tuple[tuple[object, ...], ...]], ...]:
    namespace = getattr(owner, "__dict__", None)
    if namespace is None:
        raise RuntimeError(f"canonical {label} has no class namespace")

    captured: list[
        tuple[object, str, FunctionType, tuple[tuple[object, ...], ...]]
    ] = []
    for name in method_names:
        method = namespace.get(name, _EMPTY)
        if type(method) is not _FUNCTION_TYPE:
            raise RuntimeError(f"canonical {label}.{name} is not a Python function")
        captured.append(
            (
                owner,
                name,
                method,
                _capture_function_graph(method, f"{label}.{name}"),
            )
        )
    return tuple(captured)


def _require_class_method_graph(
    graph: tuple[
        tuple[object, str, FunctionType, tuple[tuple[object, ...], ...]], ...
    ],
    label: str,
) -> None:
    for owner, name, expected_method, function_graph in graph:
        namespace = getattr(owner, "__dict__", None)
        if namespace is None or namespace.get(name, _EMPTY) is not expected_method:
            raise RuntimeError(f"{label} method dispatch authority changed: {name}")
        _require_function_graph(function_graph, f"{label}.{name}")


def _install() -> None:
    original = _precommit.publish_campaign_precommit_manifest
    graph = _capture_function_graph(original, "campaign precommit publisher")
    authority_methods = _capture_class_method_graph(
        _precommit.MonotonicWorkspaceAuthority,
        _AUTHORITY_METHOD_NAMES,
        "campaign precommit monotonic authority",
    )
    require = _require_function_graph
    require_authority = _require_class_method_graph

    def guarded_publish(*args, **kwargs):
        require(graph, "campaign precommit publisher")
        require_authority(
            authority_methods,
            "campaign precommit monotonic authority",
        )
        try:
            return original(*args, **kwargs)
        finally:
            require(graph, "campaign precommit publisher")
            require_authority(
                authority_methods,
                "campaign precommit monotonic authority",
            )

    guarded_publish.__name__ = original.__name__
    guarded_publish.__qualname__ = original.__qualname__
    guarded_publish.__doc__ = original.__doc__
    guarded_publish.__annotations__ = dict(original.__annotations__)
    _precommit.publish_campaign_precommit_manifest = guarded_publish


_install()
del _install


__all__: list[str] = []
