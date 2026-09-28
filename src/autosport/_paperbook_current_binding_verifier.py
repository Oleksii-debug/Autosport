"""Seal current PaperBook binding checks behind the sealed persistence graph.

No path binding, generation registry, or alternate persistence authority is created.
The installed public consumer function never stores positive binding authority in its
globals. Instead, each call resolves the verifier from the already-sealed PaperBook
load graph, witnesses that graph, reconstructs the original consumer with an ephemeral
globals snapshot, and injects the verified read-only capability only for that call.

Consumers that also use the owning PaperBook persistence module receive an ephemeral
facade reconstructed from that same sealed load graph. Mutable module aliases and
module attributes therefore cannot retarget transaction staging or promotion.
"""

from __future__ import annotations

from types import FunctionType, SimpleNamespace

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


def _sealed_load_graph() -> tuple[dict[str, object], FunctionType, dict[str, object]]:
    descriptor = vars(_paper.PaperBook).get("load")
    if type(descriptor) is not classmethod or type(descriptor.__func__) is not FunctionType:
        raise RuntimeError("canonical sealed PaperBook load wrapper is unavailable")
    trusted_globals = _sealed_load_trusted_globals(descriptor.__func__)
    frozen_load = trusted_globals.get("_FROZEN_LOAD")
    if type(frozen_load) is not FunctionType:
        raise RuntimeError("canonical frozen PaperBook load graph is unavailable")
    frozen_globals = frozen_load.__globals__
    if type(frozen_globals) is not dict:
        raise RuntimeError("canonical frozen PaperBook persistence globals are unavailable")
    return trusted_globals, frozen_load, frozen_globals


def _make_current_binding_resolver() -> FunctionType:
    trusted_globals, frozen_load, frozen_globals = _sealed_load_graph()
    verifier = frozen_globals.get("_require_bound_book")
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
            or frozen_globals.get("_require_bound_book") is not verifier
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
                raise ValueError(
                    "PaperBook current-binding verifier executable authority changed"
                )
            current_closure = function.__closure__
            if current_closure is None:
                if closure_values is not None:
                    raise ValueError(
                        "PaperBook current-binding verifier closure authority changed"
                    )
            else:
                if closure_values is None or len(current_closure) != len(closure_values):
                    raise ValueError(
                        "PaperBook current-binding verifier closure authority changed"
                    )
                for cell, expected in zip(current_closure, closure_values):
                    try:
                        current = cell.cell_contents
                    except ValueError as exc:
                        raise ValueError(
                            "PaperBook current-binding verifier closure authority changed"
                        ) from exc
                    if current is not expected:
                        raise ValueError(
                            "PaperBook current-binding verifier closure authority changed"
                        )
            for name, expected, expected_code in bindings:
                if globals_mapping.get(name, empty_cell) is not expected:
                    raise ValueError(
                        "PaperBook current-binding verifier global authority changed"
                    )
                if expected_code is not None and (
                    exact_type(expected) is not function_type
                    or expected.__code__ is not expected_code
                ):
                    raise ValueError(
                        "PaperBook current-binding verifier dependency executable changed"
                    )
        return verifier

    return resolve_current_binding


def _make_persistence_authority_resolver(
    required_names: tuple[str, ...],
) -> FunctionType:
    """Resolve an ephemeral facade from the already-frozen persistence graph."""

    trusted_globals, frozen_load, frozen_globals = _sealed_load_graph()
    frozen_load_code = frozen_load.__code__
    frozen_load_globals = frozen_load.__globals__
    exact_type = type
    function_type = FunctionType
    simple_namespace = SimpleNamespace
    empty_cell = _EMPTY_CELL

    missing = tuple(name for name in required_names if name not in frozen_globals)
    if missing:
        raise RuntimeError(
            "canonical frozen PaperBook persistence members are unavailable: "
            + ", ".join(missing)
        )

    snapshot_items = tuple(frozen_globals.items())
    required_witnesses = tuple(
        (
            name,
            frozen_globals[name],
            (
                frozen_globals[name].__code__
                if type(frozen_globals[name]) is FunctionType
                else None
            ),
        )
        for name in required_names
    )
    local_function_witnesses = tuple(
        (
            name,
            value,
            value.__code__,
            value.__defaults__,
            None if value.__kwdefaults__ is None else dict(value.__kwdefaults__),
            value.__closure__,
            _closure_values(value),
        )
        for name, value in snapshot_items
        if type(value) is FunctionType and value.__globals__ is frozen_globals
    )

    def fresh_cell(value: object):
        def capture():
            return value

        closure = capture.__closure__
        if closure is None:
            raise RuntimeError("PaperBook persistence closure capture failed")
        return closure[0]

    def require_graph() -> None:
        if (
            trusted_globals.get("_FROZEN_LOAD") is not frozen_load
            or exact_type(frozen_load) is not function_type
            or frozen_load.__code__ is not frozen_load_code
            or frozen_load.__globals__ is not frozen_load_globals
        ):
            raise ValueError("PaperBook persistence authority root changed")
        for name, expected, expected_code in required_witnesses:
            if frozen_globals.get(name, empty_cell) is not expected:
                raise ValueError(f"PaperBook persistence authority changed: {name}")
            if expected_code is not None and (
                exact_type(expected) is not function_type
                or expected.__code__ is not expected_code
            ):
                raise ValueError(
                    f"PaperBook persistence executable authority changed: {name}"
                )
        for (
            name,
            function,
            code,
            defaults,
            kwdefaults,
            closure,
            closure_values,
        ) in local_function_witnesses:
            if (
                frozen_globals.get(name, empty_cell) is not function
                or exact_type(function) is not function_type
                or function.__code__ is not code
                or function.__defaults__ != defaults
                or function.__kwdefaults__ != kwdefaults
                or function.__closure__ != closure
            ):
                raise ValueError(
                    f"PaperBook persistence graph executable authority changed: {name}"
                )
            current_closure = function.__closure__
            if current_closure is None:
                if closure_values is not None:
                    raise ValueError(
                        f"PaperBook persistence graph closure authority changed: {name}"
                    )
            else:
                if closure_values is None or len(current_closure) != len(closure_values):
                    raise ValueError(
                        f"PaperBook persistence graph closure authority changed: {name}"
                    )
                for cell, expected in zip(current_closure, closure_values):
                    try:
                        current = cell.cell_contents
                    except ValueError as exc:
                        raise ValueError(
                            f"PaperBook persistence graph closure authority changed: {name}"
                        ) from exc
                    if current is not expected:
                        raise ValueError(
                            f"PaperBook persistence graph closure authority changed: {name}"
                        )

    require_graph_code = require_graph.__code__
    fresh_cell_code = fresh_cell.__code__

    def resolve_persistence_authority() -> SimpleNamespace:
        if (
            exact_type(require_graph) is not function_type
            or require_graph.__code__ is not require_graph_code
            or exact_type(fresh_cell) is not function_type
            or fresh_cell.__code__ is not fresh_cell_code
        ):
            raise ValueError("PaperBook persistence resolver executable authority changed")
        require_graph()

        call_globals = dict(snapshot_items)
        for (
            name,
            _function,
            code,
            defaults,
            kwdefaults,
            _closure,
            closure_values,
        ) in local_function_witnesses:
            cloned_closure = (
                None
                if closure_values is None
                else tuple(fresh_cell(value) for value in closure_values)
            )
            clone = function_type(
                code,
                call_globals,
                name=name,
                argdefs=defaults,
                closure=cloned_closure,
            )
            if kwdefaults is not None:
                clone.__kwdefaults__ = dict(kwdefaults)
            call_globals[name] = clone

        facade = simple_namespace(
            **{name: call_globals[name] for name in required_names}
        )
        require_graph()
        return facade

    return resolve_persistence_authority


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

    persistence_resolver: FunctionType | None = None
    persistence_resolver_code: object | None = None
    if (
        "_paperbook_authority" in inner_code.co_names
        and "_paperbook_authority" in inner_globals
    ):
        _trusted_globals, _frozen_load, frozen_globals = _sealed_load_graph()
        required_names = tuple(
            sorted(
                name
                for name in inner_code.co_names
                if name != "_paperbook_authority" and name in frozen_globals
            )
        )
        persistence_resolver = _make_persistence_authority_resolver(required_names)
        persistence_resolver_code = persistence_resolver.__code__

    def guarded_consumer(*args, **kwargs):
        if (
            exact_type(resolver) is not function_type
            or resolver.__code__ is not resolver_code
        ):
            raise ValueError(
                "PaperBook current-binding resolver executable authority changed"
            )
        verifier = resolver()
        persistence_authority = None
        if persistence_resolver is not None:
            if (
                exact_type(persistence_resolver) is not function_type
                or persistence_resolver.__code__ is not persistence_resolver_code
            ):
                raise ValueError(
                    "PaperBook persistence resolver executable authority changed"
                )
            persistence_authority = persistence_resolver()

        call_globals = dict(inner_globals)
        call_globals["_REQUIRE_CURRENT_BINDING"] = verifier
        if persistence_authority is not None:
            call_globals["_paperbook_authority"] = persistence_authority
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
            if (
                exact_type(resolver) is not function_type
                or resolver.__code__ is not resolver_code
            ):
                raise ValueError(
                    "PaperBook current-binding resolver executable authority changed"
                )
            resolver()
            if persistence_resolver is not None:
                if (
                    exact_type(persistence_resolver) is not function_type
                    or persistence_resolver.__code__ is not persistence_resolver_code
                ):
                    raise ValueError(
                        "PaperBook persistence resolver executable authority changed"
                    )
                persistence_resolver()

    guarded_consumer.__name__ = inner_name
    guarded_consumer.__qualname__ = inner_qualname
    guarded_consumer.__doc__ = inner_doc
    guarded_consumer.__annotations__ = inner_annotations

    # RunTransaction stages through this sealer at module composition time. Seal its
    # sibling promotion method at that same point so both transaction persistence
    # phases consume the identical frozen PaperBook graph rather than a mutable
    # run_transaction module alias.
    if inner_qualname == "RunTransaction._stage_paper_book_snapshot":
        owner = inner_globals.get("RunTransaction")
        if type(owner) is type:
            promotion = vars(owner).get("_promote_paper_book_snapshot")
            if type(promotion) is FunctionType:
                owner._promote_paper_book_snapshot = seal_current_binding_consumer(
                    promotion
                )
            else:
                raise RuntimeError(
                    "canonical RunTransaction PaperBook promotion method is unavailable"
                )

    return guarded_consumer
