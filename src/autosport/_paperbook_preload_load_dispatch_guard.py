"""Freeze the existing PaperBook positive persistence dispatch graph.

The owning preload authority guard remains the only witness protocol and the canonical
PaperBook parser/serializer remains the only structural persistence authority. This
composition step snapshots that guard's local function graph into one closure-private
globals mapping before positive path load or durable save can be used. Later
module-global rebinding therefore cannot redirect witness verification, recovery,
authority installation, path binding, candidate hashing, witness publication, or
atomic replacement while keeping the same public ``PaperBook`` surfaces.
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
    """Freeze one external module-local function graph at its current executable state."""

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


def _capture_delegate_graph(delegate: object) -> tuple[object, ...]:
    """Capture the semantic closure/global edges used by one persistence delegate."""

    if type(delegate) is not FunctionType:
        raise RuntimeError("canonical PaperBook persistence delegate is not a Python function")
    closure = delegate.__closure__
    closure_values = () if closure is None else tuple(cell.cell_contents for cell in closure)
    global_witnesses: list[tuple[str, object, object | None]] = []
    for name in delegate.__code__.co_names:
        if name not in delegate.__globals__:
            continue
        value = delegate.__globals__[name]
        code = value.__code__ if type(value) is FunctionType else None
        global_witnesses.append((name, value, code))
    return (
        delegate,
        delegate.__code__,
        delegate.__globals__,
        closure,
        closure_values,
        tuple(global_witnesses),
    )


def _require_delegate_graph_witnesses(witnesses: tuple[tuple[object, ...], ...]) -> None:
    """Reject retargeting before any positive persistence delegate can execute."""

    for witness in witnesses:
        delegate, code, globals_mapping, closure, closure_values, globals_witnesses = witness
        if (
            type(delegate) is not FunctionType
            or delegate.__code__ is not code
            or delegate.__globals__ is not globals_mapping
            or delegate.__closure__ != closure
        ):
            raise ValueError("PaperBook persistence delegate executable authority changed")
        current_closure = delegate.__closure__
        if current_closure is None:
            if closure_values:
                raise ValueError("PaperBook persistence delegate closure authority changed")
        else:
            if len(current_closure) != len(closure_values):
                raise ValueError("PaperBook persistence delegate closure authority changed")
            for cell, expected in zip(current_closure, closure_values):
                try:
                    current = cell.cell_contents
                except ValueError as exc:
                    raise ValueError(
                        "PaperBook persistence delegate closure authority changed"
                    ) from exc
                if current is not expected:
                    raise ValueError("PaperBook persistence delegate closure authority changed")
        for name, expected, expected_code in globals_witnesses:
            if globals_mapping.get(name) is not expected:
                raise ValueError("PaperBook persistence delegate global authority changed")
            if expected_code is not None and (
                type(expected) is not FunctionType or expected.__code__ is not expected_code
            ):
                raise ValueError("PaperBook persistence delegate global executable changed")


def _descriptor_function(descriptor: object) -> FunctionType | None:
    if type(descriptor) in {classmethod, staticmethod}:
        function = descriptor.__func__
        return function if type(function) is FunctionType else None
    if type(descriptor) is FunctionType:
        return descriptor
    return None


def _capture_class_callable_witness(
    name: str,
    descriptor: object,
    function: FunctionType,
) -> tuple[object, ...]:
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


def _capture_class_callable_graph(
    owner: type,
    roots: tuple[FunctionType, ...],
) -> tuple[tuple[object, ...], ...]:
    """Capture class-dispatched callables reachable from trusted parser roots."""

    namespace = vars(owner)
    root_set = set(roots)
    seen: set[FunctionType] = set()
    pending: list[FunctionType] = []
    witnesses: list[tuple[object, ...]] = []
    for name, descriptor in namespace.items():
        function = _descriptor_function(descriptor)
        if function is None or function not in root_set or function in seen:
            continue
        seen.add(function)
        pending.append(function)
        witnesses.append(_capture_class_callable_witness(name, descriptor, function))
    if not root_set.issubset(seen):
        raise RuntimeError("canonical PaperBook parser graph root is unavailable")
    while pending:
        function = pending.pop()
        for name in function.__code__.co_names:
            descriptor = namespace.get(name, _EMPTY_CELL)
            nested = _descriptor_function(descriptor)
            if nested is None or nested in seen:
                continue
            seen.add(nested)
            pending.append(nested)
            witnesses.append(_capture_class_callable_witness(name, descriptor, nested))
    return tuple(witnesses)


def _capture_class_dispatch_graph(
    owner: type,
    roots: tuple[FunctionType, ...],
) -> tuple[tuple[object, ...], ...]:
    """Capture live class descriptors reached by detached canonical delegates."""

    namespace = vars(owner)
    seen: set[FunctionType] = set(roots)
    pending: list[FunctionType] = list(roots)
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
            witnesses.append(_capture_class_callable_witness(name, descriptor, nested))
    return tuple(witnesses)


def _capture_value_type_callable_witnesses(
    owner: type,
) -> tuple[tuple[object, ...], ...]:
    """Capture every executable descriptor on one persistence-constructed value type."""

    witnesses: list[tuple[object, ...]] = []
    for name, descriptor in vars(owner).items():
        function = _descriptor_function(descriptor)
        if function is not None:
            witnesses.append(_capture_class_callable_witness(name, descriptor, function))
    if not witnesses:
        raise RuntimeError("canonical PaperBook value type has no executable surface")
    return tuple(witnesses)


def _require_class_callable_graph_witnesses(
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
            raise ValueError("PaperBook persistence class dispatch authority changed")
        current_function = _descriptor_function(descriptor)
        if (
            current_function is not function
            or function.__code__ is not code
            or function.__globals__ is not globals_mapping
            or not _same_identity_values(_closure_values(function), closure_values)
        ):
            raise ValueError("PaperBook persistence class executable authority changed")
        for global_name, expected, expected_code, expected_closure in global_witnesses:
            if globals_mapping.get(global_name, _EMPTY_CELL) is not expected:
                raise ValueError("PaperBook persistence class global authority changed")
            if expected_code is not None:
                if type(expected) is not FunctionType or expected.__code__ is not expected_code:
                    raise ValueError("PaperBook persistence class global executable changed")
                if not _same_identity_values(_closure_values(expected), expected_closure):
                    raise ValueError("PaperBook persistence class global closure authority changed")


def _require_value_type_callable_witnesses(
    witnesses: tuple[tuple[type, tuple[tuple[object, ...], ...]], ...],
) -> None:
    for owner, callable_witnesses in witnesses:
        _require_class_callable_graph_witnesses(owner, callable_witnesses)


def _guarded_load_template(cls, path):
    _require_delegate_graph_witnesses(_LOAD_DELEGATE_GRAPH_WITNESSES)
    _require_class_callable_graph_witnesses(
        _CANONICAL_PAPER_BOOK,
        _LOAD_CLASS_CALLABLE_GRAPH_WITNESSES,
    )
    _require_value_type_callable_witnesses(_VALUE_TYPE_CALLABLE_WITNESSES)
    result = _FROZEN_LOAD(cls, path)
    _require_value_type_callable_witnesses(_VALUE_TYPE_CALLABLE_WITNESSES)
    _require_class_callable_graph_witnesses(
        _CANONICAL_PAPER_BOOK,
        _LOAD_CLASS_CALLABLE_GRAPH_WITNESSES,
    )
    _require_delegate_graph_witnesses(_LOAD_DELEGATE_GRAPH_WITNESSES)
    return result


def _guarded_save_template(self, path):
    _require_delegate_graph_witnesses(_SAVE_DELEGATE_GRAPH_WITNESSES)
    _require_class_callable_graph_witnesses(
        _CANONICAL_PAPER_BOOK,
        _SAVE_CLASS_CALLABLE_GRAPH_WITNESSES,
    )
    _require_value_type_callable_witnesses(_VALUE_TYPE_CALLABLE_WITNESSES)
    result = _FROZEN_SAVE(self, path)
    _require_value_type_callable_witnesses(_VALUE_TYPE_CALLABLE_WITNESSES)
    _require_class_callable_graph_witnesses(
        _CANONICAL_PAPER_BOOK,
        _SAVE_CLASS_CALLABLE_GRAPH_WITNESSES,
    )
    _require_delegate_graph_witnesses(_SAVE_DELEGATE_GRAPH_WITNESSES)
    return result


def _install() -> None:
    paper_book = _paper.PaperBook
    class_namespace = vars(paper_book)
    load_descriptor = class_namespace.get("load")
    save_descriptor = class_namespace.get("save")
    if type(load_descriptor) is not classmethod:
        raise RuntimeError("canonical PaperBook positive path load must remain a classmethod")
    if type(save_descriptor) is not FunctionType:
        raise RuntimeError("canonical PaperBook durable save must remain a Python function")

    original_load = load_descriptor.__func__
    guard_namespace = _guard.__dict__
    if original_load is not guard_namespace.get("_trusted_path_load"):
        raise RuntimeError("canonical PaperBook positive path load authority changed")
    if save_descriptor is not guard_namespace.get("_trusted_save"):
        raise RuntimeError("canonical PaperBook durable save authority changed")
    if type(original_load) is not FunctionType:
        raise RuntimeError("canonical PaperBook positive path load must be a Python function")

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

    frozen_load = clones.get("_trusted_path_load")
    frozen_save = clones.get("_trusted_save")
    if frozen_load is None or frozen_save is None:
        raise RuntimeError("canonical PaperBook persistence clone is unavailable")

    load_delegate_names = (
        "_LOAD_BYTES",
        "_REGISTER_OPENING",
        "_REGISTER_CAUSAL",
        "_INSTALL_OPENING",
        "_INSTALL_CAUSAL",
    )
    load_delegate_witnesses = tuple(
        _capture_delegate_graph(guard_namespace.get(name)) for name in load_delegate_names
    )
    original_save = guard_namespace.get("_ORIGINAL_SAVE")
    if type(original_save) is not FunctionType:
        raise RuntimeError("canonical PaperBook serializer authority changed")
    save_delegate_witnesses = (_capture_delegate_graph(original_save),)

    load_bytes = guard_namespace.get("_LOAD_BYTES")
    init_descriptor = class_namespace.get("__init__")
    init_function = _descriptor_function(init_descriptor)
    if type(load_bytes) is not FunctionType or init_function is None:
        raise RuntimeError("canonical PaperBook parser class graph is unavailable")
    load_class_callable_witnesses = _capture_class_callable_graph(
        paper_book,
        (load_bytes, init_function),
    )
    save_class_callable_witnesses = _capture_class_dispatch_graph(
        paper_book,
        (original_save,),
    )
    value_type_callable_witnesses = tuple(
        (owner, _capture_value_type_callable_witnesses(owner))
        for owner in (_paper.TicketLeg, _paper.PaperTicket)
    )

    wrapper_globals: dict[str, object] = dict(globals())
    wrapper_globals["_LOAD_DELEGATE_GRAPH_WITNESSES"] = load_delegate_witnesses
    wrapper_globals["_SAVE_DELEGATE_GRAPH_WITNESSES"] = save_delegate_witnesses
    wrapper_globals["_LOAD_CLASS_CALLABLE_GRAPH_WITNESSES"] = load_class_callable_witnesses
    wrapper_globals["_SAVE_CLASS_CALLABLE_GRAPH_WITNESSES"] = save_class_callable_witnesses
    wrapper_globals["_VALUE_TYPE_CALLABLE_WITNESSES"] = value_type_callable_witnesses
    wrapper_globals["_CANONICAL_PAPER_BOOK"] = paper_book
    wrapper_globals["_FROZEN_LOAD"] = frozen_load
    wrapper_globals["_FROZEN_SAVE"] = frozen_save
    wrapper_globals["_require_delegate_graph_witnesses"] = _clone_local_function(
        _require_delegate_graph_witnesses,
        trusted_globals=wrapper_globals,
    )
    wrapper_globals["_require_class_callable_graph_witnesses"] = _clone_local_function(
        _require_class_callable_graph_witnesses,
        trusted_globals=wrapper_globals,
    )
    wrapper_globals["_require_value_type_callable_witnesses"] = _clone_local_function(
        _require_value_type_callable_witnesses,
        trusted_globals=wrapper_globals,
    )
    guarded_load = _clone_local_function(
        _guarded_load_template,
        trusted_globals=wrapper_globals,
    )
    guarded_load.__name__ = "load"
    guarded_load.__qualname__ = "PaperBook.load"
    guarded_save = _clone_local_function(
        _guarded_save_template,
        trusted_globals=wrapper_globals,
    )
    guarded_save.__name__ = "save"
    guarded_save.__qualname__ = "PaperBook.save"

    paper_book.load = classmethod(guarded_load)
    paper_book.save = guarded_save


_install()
del _install
del _guarded_save_template
del _guarded_load_template
del _require_value_type_callable_witnesses
del _require_class_callable_graph_witnesses
del _capture_value_type_callable_witnesses
del _capture_class_dispatch_graph
del _capture_class_callable_graph
del _capture_class_callable_witness
del _descriptor_function
del _require_delegate_graph_witnesses
del _capture_delegate_graph
del _clone_module_function_graph
del _clone_local_function
