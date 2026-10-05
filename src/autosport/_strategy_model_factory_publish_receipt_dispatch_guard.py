"""Seal StrategyModelFactory publish-receipt dispatch after composition.

The owning publish-receipt guard remains the only receipt/transaction authority.  This
module adds fail-closed composition properties around that existing implementation:

* the low-level ledger append method is not a caller capability; it may execute only
  while the canonical manifest + final-ScientificRegistry issuer is active;
* already-installed issuer/recovery/runner/store functions retain the exact Python
  executable/global graph captured at composition; and
* authority-bearing project class methods used below those functions retain their
  exact post-composition dispatch, so an unchanged class identity cannot hide a
  replaced artifact-hash, machine-authority, or workspace-lock implementation.

No registry, artifact store, transaction protocol, lock, clock, or machine authority is
created here.  As with the merged trusted-runtime code-profile prerequisite, this is a
TRUSTED_PRODUCT_INTERPRETER composition fence, not a sandbox against arbitrary hostile
code already controlling every object inside the Python interpreter.
"""

from __future__ import annotations

from contextvars import ContextVar
from types import FunctionType

from . import _strategy_model_factory_publish_receipt_guard as _guard
from . import strategy_model_factory as _factory


_EMPTY = object()
_FUNCTION_TYPE = FunctionType


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
    """Capture the Python function graph that can redirect one installed dispatch."""

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
                raise RuntimeError(
                    f"{label} closure authority changed after composition"
                )
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


def _descriptor_functions(raw: object) -> tuple[FunctionType, ...]:
    if type(raw) is _FUNCTION_TYPE:
        return (raw,)
    if type(raw) in (staticmethod, classmethod):
        function = raw.__func__
        return (function,) if type(function) is _FUNCTION_TYPE else ()
    if type(raw) is property:
        return tuple(
            function
            for function in (raw.fget, raw.fset, raw.fdel)
            if type(function) is _FUNCTION_TYPE
        )
    return ()


def _capture_type_executable_surface(
    owner: type,
    label: str,
) -> tuple[tuple[object, ...], ...]:
    """Freeze every executable descriptor defined by one project class/MRO."""

    if type(owner) is not type:
        raise RuntimeError(f"canonical {label} is not an exact class")
    captured: list[tuple[object, ...]] = []
    for defining_type in owner.__mro__:
        if defining_type is object:
            continue
        executable_names = frozenset(
            name
            for name, raw in vars(defining_type).items()
            if _descriptor_functions(raw)
        )
        entries: list[tuple[object, ...]] = []
        for name in sorted(executable_names):
            raw = vars(defining_type)[name]
            functions = _descriptor_functions(raw)
            entries.append(
                (
                    name,
                    raw,
                    tuple((function, function.__code__) for function in functions),
                )
            )
        captured.append((defining_type, executable_names, tuple(entries)))
    return tuple(captured)


def _require_type_executable_surface(
    surface: tuple[tuple[object, ...], ...],
    label: str,
) -> None:
    for defining_type, expected_names, entries in surface:
        current_names = frozenset(
            name
            for name, raw in vars(defining_type).items()
            if _descriptor_functions(raw)
        )
        if current_names != expected_names:
            raise RuntimeError(f"{label} class dispatch authority changed")
        for name, expected_raw, functions in entries:
            if vars(defining_type).get(name, _EMPTY) is not expected_raw:
                raise RuntimeError(f"{label} class dispatch authority changed: {name}")
            for function, expected_code in functions:
                if (
                    type(function) is not _FUNCTION_TYPE
                    or function.__code__ is not expected_code
                ):
                    raise RuntimeError(
                        f"{label} class executable authority changed: {name}"
                    )


def _capture_named_class_surface(
    owner: type,
    names: tuple[str, ...],
    label: str,
) -> tuple[tuple[object, ...], ...]:
    """Freeze selected resolved methods, including inherited store primitives."""

    captured: list[tuple[object, ...]] = []
    for name in names:
        expected_resolved = getattr(owner, name, _EMPTY)
        if expected_resolved is _EMPTY:
            raise RuntimeError(f"canonical {label} is missing class surface {name}")
        defining_type = next(
            (
                base
                for base in owner.__mro__
                if name in vars(base)
            ),
            None,
        )
        if defining_type is None:
            raise RuntimeError(f"canonical {label} cannot resolve class surface {name}")
        raw = vars(defining_type)[name]
        functions = _descriptor_functions(raw)
        if not functions:
            raise RuntimeError(f"canonical {label} surface is not executable: {name}")
        captured.append(
            (
                name,
                defining_type,
                raw,
                expected_resolved,
                tuple((function, function.__code__) for function in functions),
            )
        )
    return tuple(captured)


def _require_named_class_surface(
    owner: type,
    surface: tuple[tuple[object, ...], ...],
    label: str,
) -> None:
    for name, defining_type, expected_raw, expected_resolved, functions in surface:
        if vars(defining_type).get(name, _EMPTY) is not expected_raw:
            raise RuntimeError(f"{label} class dispatch authority changed: {name}")
        if getattr(owner, name, _EMPTY) is not expected_resolved:
            raise RuntimeError(f"{label} resolved dispatch authority changed: {name}")
        for function, expected_code in functions:
            if type(function) is not _FUNCTION_TYPE or function.__code__ is not expected_code:
                raise RuntimeError(f"{label} class executable authority changed: {name}")


def _clone_function(
    function: FunctionType,
    *,
    trusted_globals: dict[str, object],
) -> FunctionType:
    """Clone one Python function over a private globals snapshot, as #1891 does."""

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


def _frozen_graph_verifier() -> FunctionType:
    """Build an independent verifier not dispatched through writable module globals."""

    trusted_globals: dict[str, object] = dict(globals())
    frozen_closure_values = _clone_function(
        _closure_values,
        trusted_globals=trusted_globals,
    )
    trusted_globals["_closure_values"] = frozen_closure_values
    return _clone_function(
        _require_function_graph,
        trusted_globals=trusted_globals,
    )


_FROZEN_REQUIRE_FUNCTION_GRAPH = _frozen_graph_verifier()


def _sealed(function: FunctionType, label: str) -> FunctionType:
    graph = _capture_function_graph(function, label)
    # Keep the ordinary verifier as a second line of defense and for readable failure
    # locality, but do not make that writable closure cell the sole trust root.
    require = _require_function_graph

    def sealed(*args, **kwargs):
        require(graph, label)
        _TRUSTED_REQUIRE(_TRUSTED_GRAPH, _TRUSTED_LABEL)
        try:
            return _TRUSTED_FUNCTION(*args, **kwargs)
        finally:
            _TRUSTED_REQUIRE(_TRUSTED_GRAPH, _TRUSTED_LABEL)
            require(graph, label)

    # Reuse the exact frozen-dispatch pattern landed by #1891: the authority-bearing
    # verifier, graph and target live in a private globals snapshot of the installed
    # clone.  Mutating the ordinary `require` closure cell therefore cannot disable
    # the independent pre/post graph check exercised by the canonical path.
    trusted_globals: dict[str, object] = dict(sealed.__globals__)
    trusted_globals["_TRUSTED_REQUIRE"] = _FROZEN_REQUIRE_FUNCTION_GRAPH
    trusted_globals["_TRUSTED_GRAPH"] = graph
    trusted_globals["_TRUSTED_LABEL"] = label
    trusted_globals["_TRUSTED_FUNCTION"] = function
    return _clone_function(sealed, trusted_globals=trusted_globals)


def _class_surface_sealed(
    function: FunctionType,
    label: str,
    require_class_surface: FunctionType,
) -> FunctionType:
    sealed_function = _sealed(function, label)

    def guarded(self, *args, **kwargs):
        _TRUSTED_REQUIRE_CLASS_SURFACE(self)
        try:
            return _TRUSTED_SEALED_FUNCTION(self, *args, **kwargs)
        finally:
            _TRUSTED_REQUIRE_CLASS_SURFACE(self)

    trusted_globals: dict[str, object] = dict(guarded.__globals__)
    trusted_globals["_TRUSTED_REQUIRE_CLASS_SURFACE"] = require_class_surface
    trusted_globals["_TRUSTED_SEALED_FUNCTION"] = sealed_function
    return _clone_function(guarded, trusted_globals=trusted_globals)


def _require_store_append_surface(store) -> None:
    """Require exact class dispatch and no instance shadow for canonical append."""

    if type(store) is not _TRUSTED_STORE_TYPE:
        raise RuntimeError("factory publish store type changed after composition")
    instance_state = getattr(store, "__dict__", None)
    if type(instance_state) is dict and "_append_publish_commit_record" in instance_state:
        raise RuntimeError("factory publish append instance dispatch changed")
    if (
        vars(_TRUSTED_STORE_TYPE).get("_append_publish_commit_record")
        is not _TRUSTED_APPEND_DISPATCH
    ):
        raise RuntimeError("factory publish append class dispatch changed")


def _install() -> None:
    store_type = _factory.FactoryArtifactStore
    runner_type = _factory.ExperimentRunner
    authority_type = _guard.MonotonicWorkspaceAuthority
    lock_type = _guard.WorkspaceEconomicLock

    original_append = vars(store_type).get("_append_publish_commit_record")
    original_record = getattr(_factory, "_record_committed_factory_publish", None)
    if original_append is not _guard._append_publish_commit_record:
        raise RuntimeError("canonical factory publish append authority changed before sealing")
    if original_record is not _guard._record_committed_factory_publish:
        raise RuntimeError("canonical factory publish issuer authority changed before sealing")

    store_core_surface = _capture_named_class_surface(
        store_type,
        ("sha256", "_stable_snapshot", "_path", "path_for_testing"),
        "factory artifact store core",
    )
    authority_surface = _capture_type_executable_surface(
        authority_type,
        "monotonic workspace authority",
    )
    lock_surface = _capture_type_executable_surface(
        lock_type,
        "workspace economic lock",
    )

    def require_class_surface(store):
        if type(store) is not store_type:
            raise RuntimeError("factory publish store type changed after composition")
        instance_state = getattr(store, "__dict__", None)
        if type(instance_state) is dict:
            for name in ("sha256", "_stable_snapshot", "_path", "path_for_testing"):
                if name in instance_state:
                    raise RuntimeError(
                        f"factory artifact store instance dispatch changed: {name}"
                    )
        _require_named_class_surface(
            store_type,
            store_core_surface,
            "factory artifact store core",
        )
        _require_type_executable_surface(
            authority_surface,
            "monotonic workspace authority",
        )
        _require_type_executable_surface(
            lock_surface,
            "workspace economic lock",
        )

    require_class_surface = _sealed(
        require_class_surface,
        "factory publish authority class-surface verifier",
    )

    # The low-level append implementation deliberately does not reread ScientificRegistry:
    # that check belongs to the existing canonical issuer. Keep append private in fact,
    # not merely by naming convention, through a product-owned per-context capability.
    append_capability = object()
    active_append: ContextVar[object | None] = ContextVar(
        "autosport_factory_publish_append_capability_v1",
        default=None,
    )
    sealed_append = _sealed(original_append, "factory publish append")
    sealed_record = _sealed(original_record, "factory publish issuer")

    def gated_append(self, transaction):
        if _TRUSTED_ACTIVE_APPEND.get() is not _TRUSTED_APPEND_CAPABILITY:
            raise ValueError(
                "factory publish append requires the canonical transaction issuer"
            )
        _TRUSTED_REQUIRE_CLASS_SURFACE(self)
        try:
            return _TRUSTED_SEALED_APPEND(self, transaction)
        finally:
            _TRUSTED_REQUIRE_CLASS_SURFACE(self)

    gated_globals: dict[str, object] = dict(gated_append.__globals__)
    gated_globals["_TRUSTED_ACTIVE_APPEND"] = active_append
    gated_globals["_TRUSTED_APPEND_CAPABILITY"] = append_capability
    gated_globals["_TRUSTED_REQUIRE_CLASS_SURFACE"] = require_class_surface
    gated_globals["_TRUSTED_SEALED_APPEND"] = sealed_append
    gated_append = _clone_function(gated_append, trusted_globals=gated_globals)

    append_surface_globals: dict[str, object] = dict(
        _require_store_append_surface.__globals__
    )
    append_surface_globals["_TRUSTED_STORE_TYPE"] = store_type
    append_surface_globals["_TRUSTED_APPEND_DISPATCH"] = gated_append
    require_append_surface = _clone_function(
        _require_store_append_surface,
        trusted_globals=append_surface_globals,
    )

    def canonical_record(registry, store):
        # Keep this closure identity as an additional mutation witness. The actual
        # authority-bearing target and append-surface verifier live in private globals.
        if sealed_record is not _TRUSTED_SEALED_RECORD:
            raise RuntimeError("factory publish issuer closure changed after composition")
        _TRUSTED_REQUIRE_CLASS_SURFACE(store)
        _TRUSTED_REQUIRE_APPEND_SURFACE(store)
        token = _TRUSTED_ACTIVE_APPEND.set(_TRUSTED_APPEND_CAPABILITY)
        try:
            result = _TRUSTED_SEALED_RECORD(registry, store)
            _TRUSTED_REQUIRE_CLASS_SURFACE(store)
            _TRUSTED_REQUIRE_APPEND_SURFACE(store)
            return result
        finally:
            _TRUSTED_ACTIVE_APPEND.reset(token)

    record_globals: dict[str, object] = dict(canonical_record.__globals__)
    record_globals["_TRUSTED_SEALED_RECORD"] = sealed_record
    record_globals["_TRUSTED_REQUIRE_CLASS_SURFACE"] = require_class_surface
    record_globals["_TRUSTED_REQUIRE_APPEND_SURFACE"] = require_append_surface
    record_globals["_TRUSTED_ACTIVE_APPEND"] = active_append
    record_globals["_TRUSTED_APPEND_CAPABILITY"] = append_capability
    canonical_record = _clone_function(canonical_record, trusted_globals=record_globals)

    # Route both public composition names and the owning guard's internal recovery
    # aliases through the same private capability. A caller must not bypass the
    # class gate by invoking the guard module's original append function directly.
    store_type._append_publish_commit_record = gated_append
    _guard._append_publish_commit_record = gated_append
    _guard._record_committed_factory_publish = canonical_record
    _factory._record_committed_factory_publish = canonical_record

    store_surfaces = (
        ("_publish_commit_ledger_path", _guard._publish_commit_ledger_path),
        ("_publish_commit_digest", _guard._publish_commit_digest),
        ("_validated_publish_artifacts", _guard._validated_publish_artifacts),
        ("_publish_commit_state_sha256", _guard._publish_commit_state_sha256),
        ("_publish_commit_authority", _guard._publish_commit_authority),
        ("_recover_publish_commit_authority", _guard._recover_publish_commit_authority),
        ("_read_publish_commit_ledger", _guard._read_publish_commit_ledger),
        ("publication_receipt", _guard._publication_receipt),
    )
    for name, expected in store_surfaces:
        current = vars(store_type).get(name)
        if current is not expected:
            raise RuntimeError(f"canonical factory receipt store surface changed: {name}")
        setattr(
            store_type,
            name,
            _class_surface_sealed(
                current,
                f"factory receipt store {name}",
                require_class_surface,
            ),
        )

    factory_surfaces = (
        (
            "_unlink_transaction_manifest",
            _guard._guarded_unlink_transaction_manifest,
            "factory publish manifest cleanup",
        ),
        (
            "_recover_interrupted_factory_publish",
            _guard._recover_interrupted_factory_publish,
            "factory publish recovery",
        ),
    )
    for name, expected, label in factory_surfaces:
        current = getattr(_factory, name, None)
        if current is not expected:
            raise RuntimeError(f"canonical factory publish surface changed: {name}")
        setattr(_factory, name, _sealed(current, label))

    runner_surfaces = (
        ("run_policy_candidate", _guard._run_policy_candidate),
        ("run_baseline_candidate", _guard._run_baseline_candidate),
    )
    for name, expected in runner_surfaces:
        current = vars(runner_type).get(name)
        if current is not expected:
            raise RuntimeError(f"canonical factory runner surface changed: {name}")
        setattr(runner_type, name, _sealed(current, f"factory runner {name}"))


_install()
del _install


__all__: list[str] = []
