"""Extend PaperBook persistence witnesses to transitive stdlib executables.

The module-member freeze replaces mutable stdlib modules with narrow tuple-backed
facades and detaches the top-level Python callables used by canonical persistence.
A top-level clone is still insufficient when that callable reaches a shared mutable
Python class, nested Python helper, module member, or a name that could later be
shadowed in its globals mapping. This module composes one more witness into the
already-installed positive load/save verifier. It snapshots the transitive Python
executable graph reachable from the frozen persistence surfaces and verifies that
same graph before and after each positive persistence dispatch.

Mutable non-executable stdlib state (for example tempfile's lazy candidate-name state)
is deliberately not frozen by this verifier. The invariant is executable dispatch,
not incidental library cache identity.

No parser, serializer, store, journal, witness root, or economic authority is added.
The canonical parser/serializer and independent witness protocol remain unchanged.
"""

from __future__ import annotations

from types import FunctionType, ModuleType

from . import _paperbook_preload_authority_guard as _guard
from . import paper as _paper


# Runtime verifier primitives are explicit module bindings so the later wrapper-helper
# seal witnesses their exact identities instead of leaving ambient builtin fallbacks.
_EXT_TYPE = type
_EXT_FUNCTION_TYPE = FunctionType
_EXT_MODULE_TYPE = ModuleType
_EXT_DICT_GET = dict.get
_EXT_OBJECT_GETATTRIBUTE = object.__getattribute__
_EXT_TYPE_GETATTRIBUTE = type.__getattribute__
_EXT_MODULE_GETATTRIBUTE = ModuleType.__getattribute__
_EXT_TUPLE = tuple
_EXT_LEN = len
_EXT_ZIP = zip
_EXT_VALUE_ERROR = ValueError
_EXT_FAILURE = ValueError
_EXT_EMPTY = object()

# Populated exactly once by _install before the wrapper-helper seal snapshots this
# module's verifier graph. Later replacement is therefore tamper evidence.
_ORIGINAL_VALUE_TYPE_VERIFIER: FunctionType | None = None
_EXTERNAL_EXECUTABLE_GRAPH_WITNESSES: tuple[object, ...] = ()


def _descriptor_functions(descriptor: object) -> tuple[FunctionType, ...]:
    if type(descriptor) is FunctionType:
        return (descriptor,)
    if type(descriptor) in {classmethod, staticmethod}:
        function = descriptor.__func__
        return (function,) if type(function) is FunctionType else ()
    if type(descriptor) is property:
        return tuple(
            function
            for function in (descriptor.fget, descriptor.fset, descriptor.fdel)
            if type(function) is FunctionType
        )
    return ()


def _descriptor_function(descriptor: object) -> FunctionType | None:
    functions = _descriptor_functions(descriptor)
    return functions[0] if len(functions) == 1 else None


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


def _capture_is_executable(value: object) -> bool:
    """Return whether a binding can directly redirect executable dispatch."""

    return (
        type(value) is FunctionType
        or type(value) is ModuleType
        or isinstance(value, type)
        or callable(value)
    )


def _capture_external_executable_graph(
    roots: tuple[object, ...],
    *,
    empty_cell: object,
) -> tuple[object, ...]:
    """Snapshot Python executable state reachable from frozen persistence members."""

    function_nodes: list[tuple[object, ...]] = []
    class_nodes: list[tuple[object, ...]] = []
    pending: list[object] = list(roots)
    seen_functions: set[FunctionType] = set()
    seen_classes: set[type] = set()

    def enqueue(value: object) -> None:
        if type(value) is FunctionType:
            if value not in seen_functions:
                pending.append(value)
            return
        if isinstance(value, type) and getattr(value, "__module__", "builtins") != "builtins":
            if value not in seen_classes:
                pending.append(value)
            return
        if isinstance(value, tuple):
            # FrozenSurface is a tuple of (name, value) pairs. Recurse through nested
            # surfaces without invoking its mutable Python-level iterator override.
            for item in tuple.__iter__(value):
                if (
                    isinstance(item, tuple)
                    and len(item) == 2
                    and isinstance(item[0], str)
                ):
                    enqueue(item[1])

    while pending:
        value = pending.pop()
        if type(value) is FunctionType:
            function = value
            if function in seen_functions:
                continue
            seen_functions.add(function)
            globals_mapping = function.__globals__
            builtins_mapping = function.__builtins__
            name_witnesses: list[tuple[object, ...]] = []
            for name in function.__code__.co_names:
                if name in globals_mapping:
                    expected = globals_mapping[name]
                    if not _capture_is_executable(expected):
                        # tempfile and a few other stdlib modules legitimately mutate
                        # lazy data/cache globals during normal positive operation.
                        continue
                    expected_code = (
                        expected.__code__ if type(expected) is FunctionType else None
                    )
                    expected_closure = (
                        _closure_values(expected, empty_cell=empty_cell)
                        if type(expected) is FunctionType
                        else None
                    )
                    module_members: list[tuple[object, ...]] = []
                    if type(expected) is ModuleType:
                        module_namespace = vars(expected)
                        for member_name in function.__code__.co_names:
                            if member_name not in module_namespace:
                                continue
                            member = module_namespace[member_name]
                            if not _capture_is_executable(member):
                                continue
                            member_code = (
                                member.__code__ if type(member) is FunctionType else None
                            )
                            member_closure = (
                                _closure_values(member, empty_cell=empty_cell)
                                if type(member) is FunctionType
                                else None
                            )
                            module_members.append(
                                (
                                    member_name,
                                    member,
                                    member_code,
                                    member_closure,
                                )
                            )
                            enqueue(member)
                    enqueue(expected)
                    name_witnesses.append(
                        (
                            name,
                            "global",
                            expected,
                            expected_code,
                            expected_closure,
                            tuple(module_members),
                        )
                    )
                    continue

                builtin_value = dict.get(builtins_mapping, name, empty_cell)
                if builtin_value is empty_cell:
                    # Attribute names also occur in co_names. A missing global/builtin
                    # therefore is not itself an execution dependency and is ignored.
                    continue
                builtin_code = (
                    builtin_value.__code__
                    if type(builtin_value) is FunctionType
                    else None
                )
                builtin_closure = (
                    _closure_values(builtin_value, empty_cell=empty_cell)
                    if type(builtin_value) is FunctionType
                    else None
                )
                enqueue(builtin_value)
                name_witnesses.append(
                    (
                        name,
                        "builtin",
                        builtin_value,
                        builtin_code,
                        builtin_closure,
                        (),
                    )
                )

            function_nodes.append(
                (
                    function,
                    function.__code__,
                    globals_mapping,
                    function.__closure__,
                    _closure_values(function, empty_cell=empty_cell),
                    builtins_mapping,
                    tuple(name_witnesses),
                )
            )
            continue

        if isinstance(value, type) and getattr(value, "__module__", "builtins") != "builtins":
            owner = value
            if owner in seen_classes:
                continue
            seen_classes.add(owner)
            namespace = type.__getattribute__(owner, "__dict__")
            descriptor_witnesses: list[tuple[str, object]] = []
            for name, descriptor in namespace.items():
                functions = _descriptor_functions(descriptor)
                if not functions:
                    continue
                descriptor_witnesses.append((name, descriptor))
                for function in functions:
                    enqueue(function)
            class_nodes.append(
                (
                    owner,
                    type.__getattribute__(owner, "__bases__"),
                    tuple(descriptor_witnesses),
                )
            )
            continue

        enqueue(value)

    return (tuple(function_nodes), tuple(class_nodes))


def _same_external_closure_values(
    function: FunctionType,
    expected: tuple[object, ...] | None,
) -> bool:
    current = _EXT_OBJECT_GETATTRIBUTE(function, "__closure__")
    if current is None or expected is None:
        return current is None and expected is None
    current_values: list[object] = []
    for cell in current:
        try:
            current_values.append(cell.cell_contents)
        except _EXT_VALUE_ERROR:
            current_values.append(_EXT_EMPTY)
    if _EXT_LEN(current_values) != _EXT_LEN(expected):
        return False
    for current_value, expected_value in _EXT_ZIP(current_values, expected):
        if current_value is not expected_value:
            return False
    return True


def _require_external_function_state(
    function: object,
    expected_code: object | None,
    expected_closure: tuple[object, ...] | None,
) -> None:
    if expected_code is None:
        return
    if (
        _EXT_TYPE(function) is not _EXT_FUNCTION_TYPE
        or _EXT_OBJECT_GETATTRIBUTE(function, "__code__") is not expected_code
        or not _same_external_closure_values(function, expected_closure)
    ):
        raise _EXT_FAILURE(
            "PaperBook persistence transitive executable authority changed"
        )


def _require_external_executable_graph(graph: tuple[object, ...]) -> None:
    """Reject transitive code/global/class retargeting before positive dispatch."""

    function_nodes, class_nodes = graph
    for node in function_nodes:
        (
            function,
            expected_code,
            globals_mapping,
            expected_closure,
            expected_closure_values,
            builtins_mapping,
            name_witnesses,
        ) = node
        if (
            _EXT_TYPE(function) is not _EXT_FUNCTION_TYPE
            or _EXT_OBJECT_GETATTRIBUTE(function, "__code__") is not expected_code
            or _EXT_OBJECT_GETATTRIBUTE(function, "__globals__") is not globals_mapping
            or _EXT_OBJECT_GETATTRIBUTE(function, "__closure__") is not expected_closure
            or _EXT_OBJECT_GETATTRIBUTE(function, "__builtins__") is not builtins_mapping
            or not _same_external_closure_values(function, expected_closure_values)
        ):
            raise _EXT_FAILURE(
                "PaperBook persistence transitive function authority changed"
            )

        for witness in name_witnesses:
            (
                name,
                source,
                expected,
                expected_name_code,
                expected_name_closure,
                module_members,
            ) = witness
            current_global = _EXT_DICT_GET(globals_mapping, name, _EXT_EMPTY)
            if source == "global":
                if current_global is not expected:
                    raise _EXT_FAILURE(
                        "PaperBook persistence transitive global authority changed"
                    )
            else:
                if (
                    current_global is not _EXT_EMPTY
                    or _EXT_DICT_GET(builtins_mapping, name, _EXT_EMPTY) is not expected
                ):
                    raise _EXT_FAILURE(
                        "PaperBook persistence transitive builtin authority changed"
                    )

            _require_external_function_state(
                expected,
                expected_name_code,
                expected_name_closure,
            )
            for member_witness in module_members:
                (
                    member_name,
                    expected_member,
                    expected_member_code,
                    expected_member_closure,
                ) = member_witness
                module_namespace = _EXT_MODULE_GETATTRIBUTE(expected, "__dict__")
                if _EXT_DICT_GET(module_namespace, member_name, _EXT_EMPTY) is not expected_member:
                    raise _EXT_FAILURE(
                        "PaperBook persistence transitive module-member authority changed"
                    )
                _require_external_function_state(
                    expected_member,
                    expected_member_code,
                    expected_member_closure,
                )

    for owner, expected_bases, descriptor_witnesses in class_nodes:
        if _EXT_TYPE_GETATTRIBUTE(owner, "__bases__") is not expected_bases:
            raise _EXT_FAILURE(
                "PaperBook persistence transitive class authority changed"
            )
        namespace = _EXT_TYPE_GETATTRIBUTE(owner, "__dict__")
        for name, expected_descriptor in descriptor_witnesses:
            if namespace.get(name, _EXT_EMPTY) is not expected_descriptor:
                raise _EXT_FAILURE(
                    "PaperBook persistence transitive class dispatch authority changed"
                )


def _require_combined_value_type_callable_witnesses(
    witnesses: tuple[tuple[type, tuple[tuple[object, ...], ...]], ...],
) -> None:
    original = _ORIGINAL_VALUE_TYPE_VERIFIER
    if _EXT_TYPE(original) is not _EXT_FUNCTION_TYPE:
        raise _EXT_FAILURE("PaperBook persistence value-type verifier authority changed")
    original(witnesses)
    graph = _EXTERNAL_EXECUTABLE_GRAPH_WITNESSES
    if _EXT_TYPE(graph) is not _EXT_TUPLE:
        raise _EXT_FAILURE("PaperBook persistence transitive witness graph changed")
    _require_external_executable_graph(graph)


def _install() -> None:
    global _ORIGINAL_VALUE_TYPE_VERIFIER
    global _EXTERNAL_EXECUTABLE_GRAPH_WITNESSES

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
    original_value_type_verifier = wrapper_globals.get(
        "_require_value_type_callable_witnesses"
    )
    if (
        type(existing) is not tuple
        or empty_cell is None
        or type(original_value_type_verifier) is not FunctionType
    ):
        raise RuntimeError("PaperBook persistence value-type witness graph is unavailable")

    guard_surfaces = (_guard.json, _guard.hashlib, _guard.os, _guard.tempfile)
    paper_surfaces = (_paper.json, _paper.os, _paper.tempfile)
    surface_type = type(guard_surfaces[0])
    if any(type(surface) is not surface_type for surface in (*guard_surfaces, *paper_surfaces)):
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

    # Capture payloads from both the independent witness side and the canonical
    # parser/serializer side. Nested FrozenSurface values are traversed recursively.
    _EXTERNAL_EXECUTABLE_GRAPH_WITNESSES = _capture_external_executable_graph(
        (*guard_surfaces, *paper_surfaces),
        empty_cell=empty_cell,
    )
    _ORIGINAL_VALUE_TYPE_VERIFIER = original_value_type_verifier
    wrapper_globals["_require_value_type_callable_witnesses"] = (
        _require_combined_value_type_callable_witnesses
    )


_install()
del _install
