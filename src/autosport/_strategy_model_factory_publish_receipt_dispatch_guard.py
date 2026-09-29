"""Seal StrategyModelFactory publish-receipt dispatch after composition.

The owning publish-receipt guard remains the only receipt/transaction authority.  This
module adds two fail-closed composition properties around that existing implementation:

* the low-level ledger append method is not a caller capability; it may execute only
  while the canonical manifest + final-ScientificRegistry issuer is active; and
* already-installed issuer/recovery/runner/store functions retain the exact Python
  executable/global graph captured at composition, so later module-global rebinding
  cannot retarget a positive publication path while keeping the public wrapper object.

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


def _install() -> None:
    store_type = _factory.FactoryArtifactStore
    runner_type = _factory.ExperimentRunner

    original_append = vars(store_type).get("_append_publish_commit_record")
    original_record = getattr(_factory, "_record_committed_factory_publish", None)
    if original_append is not _guard._append_publish_commit_record:
        raise RuntimeError("canonical factory publish append authority changed before sealing")
    if original_record is not _guard._record_committed_factory_publish:
        raise RuntimeError("canonical factory publish issuer authority changed before sealing")

    # The low-level append implementation deliberately does not reread ScientificRegistry:
    # that check belongs to the existing canonical issuer.  Keep append private in fact,
    # not merely by naming convention, through a closure-owned per-context capability.
    append_capability = object()
    active_append: ContextVar[object | None] = ContextVar(
        "autosport_factory_publish_append_capability_v1",
        default=None,
    )
    sealed_append = _sealed(original_append, "factory publish append")
    sealed_record = _sealed(original_record, "factory publish issuer")

    def gated_append(self, transaction):
        if active_append.get() is not append_capability:
            raise ValueError(
                "factory publish append requires the canonical transaction issuer"
            )
        return sealed_append(self, transaction)

    def canonical_record(registry, store):
        token = active_append.set(append_capability)
        try:
            return sealed_record(registry, store)
        finally:
            active_append.reset(token)

    # Route both public composition names and the owning guard's internal recovery
    # aliases through the same private capability.  A caller must not bypass the
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
        setattr(store_type, name, _sealed(current, f"factory receipt store {name}"))

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
