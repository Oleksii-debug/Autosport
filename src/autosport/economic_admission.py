from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import FunctionType, MappingProxyType

from .domain import MarketEvent, PaperTicket, TicketLeg
from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalStore
from .monotonic_workspace_authority import (
    AuthorityRecovery,
    MonotonicWorkspaceAuthority,
)
from .paper import PaperBook
from . import _paperbook_preload_authority_guard as _paperbook_authority
from .recovery import transaction_history_requires_recovery
from .risk import (
    PaperRiskPolicy,
    ProposedTicketRiskContext,
    RiskDecision,
    RiskOfRuinEvidence,
)
from .risk_day_window import (
    ProductDayRiskWindow,
    ProductDayRiskWindowStore,
    RiskDayWindowMismatchError,
)
from .risk_turnover_evidence import PaperDayTurnoverEvidence, PaperDayTurnoverResolver
from .run_registry import RunRegistry, UnresolvedExperimentError
from .run_transaction import RunTransaction
from .workspace_lock import WorkspaceEconomicLock


_ECONOMIC_GOAL_FILE_NAME = EconomicGoalStore.FILE_NAME
_ECONOMIC_GOAL_STORE_NEW = EconomicGoalStore.__new__
_ECONOMIC_GOAL_STORE_NEW_CODE = getattr(_ECONOMIC_GOAL_STORE_NEW, "__code__", None)
_ECONOMIC_GOAL_STORE_INIT = EconomicGoalStore.__init__
_ECONOMIC_GOAL_STORE_INIT_CODE = getattr(_ECONOMIC_GOAL_STORE_INIT, "__code__", None)

_ADMISSION_PATH_NEW = Path.__new__
_ADMISSION_PATH_NEW_CODE = getattr(_ADMISSION_PATH_NEW, "__code__", None)
_ADMISSION_PATH_INIT = Path.__init__
_ADMISSION_PATH_INIT_CODE = getattr(_ADMISSION_PATH_INIT, "__code__", None)
_ADMISSION_PATH_EXPANDUSER = Path.expanduser
_ADMISSION_PATH_EXPANDUSER_CODE = getattr(_ADMISSION_PATH_EXPANDUSER, "__code__", None)
_ADMISSION_PATH_RESOLVE = Path.resolve
_ADMISSION_PATH_RESOLVE_CODE = getattr(_ADMISSION_PATH_RESOLVE, "__code__", None)
_ADMISSION_PATH_TRUEDIV = Path.__truediv__
_ADMISSION_PATH_TRUEDIV_CODE = getattr(_ADMISSION_PATH_TRUEDIV, "__code__", None)
_ADMISSION_PATH_LSTAT = Path.lstat
_ADMISSION_PATH_LSTAT_CODE = getattr(_ADMISSION_PATH_LSTAT, "__code__", None)
_ADMISSION_PATH_ITERDIR = Path.iterdir
_ADMISSION_PATH_ITERDIR_CODE = getattr(_ADMISSION_PATH_ITERDIR, "__code__", None)
_ADMISSION_PATH_EXISTS = Path.exists
_ADMISSION_PATH_EXISTS_CODE = getattr(_ADMISSION_PATH_EXISTS, "__code__", None)
_ADMISSION_CANONICAL_PATH_TYPE = type(Path())


def _capture_instance_state_class_witnesses(
    owner: type,
    names: tuple[str, ...],
) -> tuple[tuple[str, bool, object], ...]:
    """Capture whether canonical instance-state names exist on the mutable class."""

    return tuple(
        (name, name in owner.__dict__, owner.__dict__.get(name))
        for name in names
    )


def _require_instance_state_class_witnesses(
    owner: type,
    witnesses: tuple[tuple[str, bool, object], ...],
    *,
    error: str,
) -> None:
    """Reject class data descriptors that can shadow trusted instance state."""

    for name, expected_present, expected_value in witnesses:
        if (
            (name in owner.__dict__) is not expected_present
            or owner.__dict__.get(name) is not expected_value
        ):
            raise RuntimeError(error)


_WORKSPACE_LOCK_STATE_WITNESSES = _capture_instance_state_class_witnesses(
    WorkspaceEconomicLock,
    ("workspace", "path", "_handle"),
)
_PRODUCT_DAY_STORE_STATE_WITNESSES = _capture_instance_state_class_witnesses(
    ProductDayRiskWindowStore,
    ("workspace", "state_path", "_clock", "_authority"),
)
_ECONOMIC_GOAL_STORE_STATE_WITNESSES = _capture_instance_state_class_witnesses(
    EconomicGoalStore,
    ("workspace", "path"),
)
_RUN_REGISTRY_STATE_WITNESSES = _capture_instance_state_class_witnesses(
    RunRegistry,
    ("path",),
)
_RUN_TRANSACTION_STATE_WITNESSES = _capture_instance_state_class_witnesses(
    RunTransaction,
    (
        "workspace",
        "run_id",
        "root",
        "manifest_path",
        "run_ledger_path",
        "staged_book_path",
        "staged_ledger_path",
        "staged_summary_path",
        "_identity",
    ),
)
_MONOTONIC_AUTHORITY_STATE_WITNESSES = _capture_instance_state_class_witnesses(
    MonotonicWorkspaceAuthority,
    (
        "workspace",
        "domain",
        "key",
        "authority_root",
        "workspace_binding",
        "workspace_instance_id",
        "authority_root_selection",
        "authority_root_binding_path",
        "workspace_binding_path",
        "namespace_sha256",
        "authority_root_activation_path",
        "journal_dir",
        "records_dir",
        "namespace_marker_path",
    ),
)


# The admission critical section is only atomic while the exact reviewed
# WorkspaceEconomicLock executable graph remains installed. Freezing this module's
# globals preserves the class object, not its mutable Python class attributes, so
# witness the lock dispatch before entering the economic mutation boundary.
_WORKSPACE_LOCK_FILE_NAME = WorkspaceEconomicLock.FILE_NAME
_WORKSPACE_LOCK_METHOD_NAMES = (
    "__init__",
    "acquire",
    "release",
    "__enter__",
    "__exit__",
    "_open_lock_handle",
    "_open_new_lock_handle",
    "_validate_existing_lock_path",
    "_validate_open_handle_identity",
    "_require_regular_file",
    "_require_single_link",
    "_lock_handle",
    "_unlock_handle",
)
_WORKSPACE_LOCK_DISPATCH_WITNESSES = tuple(
    (
        name,
        WorkspaceEconomicLock.__dict__[name],
        (
            WorkspaceEconomicLock.__dict__[name].__func__.__code__
            if type(WorkspaceEconomicLock.__dict__[name]) is staticmethod
            else WorkspaceEconomicLock.__dict__[name].__code__
        ),
    )
    for name in _WORKSPACE_LOCK_METHOD_NAMES
)
_WORKSPACE_LOCK_GLOBAL_WITNESSES = tuple(
    (
        name,
        (
            WorkspaceEconomicLock.__dict__[name].__func__.__globals__
            if type(WorkspaceEconomicLock.__dict__[name]) is staticmethod
            else WorkspaceEconomicLock.__dict__[name].__globals__
        ),
        tuple(
            (global_name, method_globals[global_name])
            for global_name in method_code.co_names
            if global_name in method_globals
        ),
    )
    for name in _WORKSPACE_LOCK_METHOD_NAMES
    for method_code, method_globals in (
        (
            (
                WorkspaceEconomicLock.__dict__[name].__func__.__code__
                if type(WorkspaceEconomicLock.__dict__[name]) is staticmethod
                else WorkspaceEconomicLock.__dict__[name].__code__
            ),
            (
                WorkspaceEconomicLock.__dict__[name].__func__.__globals__
                if type(WorkspaceEconomicLock.__dict__[name]) is staticmethod
                else WorkspaceEconomicLock.__dict__[name].__globals__
            ),
        ),
    )
)


def _require_workspace_lock_dispatch() -> None:
    _require_instance_state_class_witnesses(
        WorkspaceEconomicLock,
        _WORKSPACE_LOCK_STATE_WITNESSES,
        error="workspace economic lock state authority changed",
    )
    if WorkspaceEconomicLock.__dict__.get("FILE_NAME") is not _WORKSPACE_LOCK_FILE_NAME:
        raise RuntimeError("workspace economic lock authority changed")
    for name, expected_descriptor, expected_code in _WORKSPACE_LOCK_DISPATCH_WITNESSES:
        current_descriptor = WorkspaceEconomicLock.__dict__.get(name)
        if current_descriptor is not expected_descriptor:
            raise RuntimeError("workspace economic lock executable authority changed")
        current_function = (
            current_descriptor.__func__
            if type(current_descriptor) is staticmethod
            else current_descriptor
        )
        if (
            type(current_function) is not FunctionType
            or current_function.__code__ is not expected_code
        ):
            raise RuntimeError("workspace economic lock executable authority changed")
    for name, expected_globals, expected_bindings in _WORKSPACE_LOCK_GLOBAL_WITNESSES:
        current_descriptor = WorkspaceEconomicLock.__dict__.get(name)
        current_function = (
            current_descriptor.__func__
            if type(current_descriptor) is staticmethod
            else current_descriptor
        )
        if (
            type(current_function) is not FunctionType
            or current_function.__globals__ is not expected_globals
        ):
            raise RuntimeError("workspace economic lock dependency authority changed")
        for global_name, expected_binding in expected_bindings:
            if (
                global_name not in expected_globals
                or expected_globals[global_name] is not expected_binding
            ):
                raise RuntimeError("workspace economic lock dependency authority changed")


def _capture_sealed_wrapper_authority(
    function: FunctionType,
) -> tuple[
    tuple[tuple[str, object, object], ...],
    tuple[tuple[str, object, object | None], ...],
]:
    """Snapshot one installed sealed wrapper's closure-owned dependency graph."""

    if type(function) is not FunctionType:
        raise RuntimeError("sealed authority wrapper must be a Python function")
    closure = function.__closure__
    freevars = function.__code__.co_freevars
    if closure is None or len(closure) != len(freevars):
        raise RuntimeError("sealed authority wrapper closure is unavailable")

    closure_witness: list[tuple[str, object, object]] = []
    closure_values: dict[str, object] = {}
    for name, cell in zip(freevars, closure, strict=True):
        try:
            value = cell.cell_contents
        except ValueError as exc:
            raise RuntimeError("sealed authority wrapper closure is incomplete") from exc
        closure_witness.append((name, cell, value))
        closure_values[name] = value

    source_function = closure_values.get("function")
    frozen_globals = closure_values.get("frozen_globals")
    if type(source_function) is not FunctionType or type(frozen_globals) is not dict:
        raise RuntimeError("sealed authority wrapper dependency graph is unavailable")

    bindings: dict[str, tuple[object, object | None]] = {}
    pending = [source_function]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in visited:
            continue
        visited.add(marker)
        for global_name in current.__code__.co_names:
            if global_name not in frozen_globals:
                continue
            value = frozen_globals[global_name]
            if global_name not in bindings:
                bindings[global_name] = (
                    value,
                    value.__code__ if type(value) is FunctionType else None,
                )
            if (
                type(value) is FunctionType
                and value.__globals__ is frozen_globals
            ):
                pending.append(value)

    binding_witness = tuple(
        (name, bindings[name][0], bindings[name][1])
        for name in sorted(bindings)
    )
    return tuple(closure_witness), binding_witness


def _require_sealed_wrapper_authority(
    function: FunctionType,
    *,
    expected_closure: tuple[tuple[str, object, object], ...],
    expected_bindings: tuple[tuple[str, object, object | None], ...],
    error: str,
) -> None:
    """Reject closure-cell, frozen-global and helper-code mutation."""

    closure = function.__closure__
    if closure is None or len(closure) != len(expected_closure):
        raise RuntimeError(error)

    frozen_globals: dict[str, object] | None = None
    for index, (name, expected_cell, expected_value) in enumerate(expected_closure):
        current_cell = closure[index]
        if current_cell is not expected_cell:
            raise RuntimeError(error)
        try:
            current_value = current_cell.cell_contents
        except ValueError as exc:
            raise RuntimeError(error) from exc
        if current_value is not expected_value:
            raise RuntimeError(error)
        if name == "frozen_globals":
            if type(current_value) is not dict:
                raise RuntimeError(error)
            frozen_globals = current_value

    if frozen_globals is None:
        raise RuntimeError(error)
    for name, expected_value, expected_code in expected_bindings:
        if name not in frozen_globals:
            raise RuntimeError(error)
        current_value = frozen_globals[name]
        if current_value is not expected_value:
            raise RuntimeError(error)
        if expected_code is not None:
            if (
                type(current_value) is not FunctionType
                or current_value.__code__ is not expected_code
            ):
                raise RuntimeError(error)


def _class_descriptor_function(descriptor: object) -> FunctionType | None:
    if type(descriptor) is FunctionType:
        return descriptor
    if type(descriptor) in {classmethod, staticmethod}:
        function = descriptor.__func__
        return function if type(function) is FunctionType else None
    return None


def _authority_descriptor_function(descriptor: object) -> FunctionType | None:
    """Return executable code for an authority-bearing method/property descriptor."""

    if type(descriptor) is property:
        function = descriptor.fget
        return function if type(function) is FunctionType else None
    return _class_descriptor_function(descriptor)


def _capture_executable_descriptor_witnesses(
    owner: type,
    names: tuple[str, ...],
) -> tuple[tuple[str, object, FunctionType, object], ...]:
    witnesses: list[tuple[str, object, FunctionType, object]] = []
    for name in names:
        descriptor = owner.__dict__.get(name)
        function = _authority_descriptor_function(descriptor)
        if function is None:
            raise RuntimeError("authority executable descriptor is unavailable")
        witnesses.append((name, descriptor, function, function.__code__))
    return tuple(witnesses)


def _require_executable_descriptor_witnesses(
    owner: type,
    witnesses: tuple[tuple[str, object, FunctionType, object], ...],
    *,
    error: str,
) -> None:
    for name, expected_descriptor, expected_function, expected_code in witnesses:
        current_descriptor = owner.__dict__.get(name)
        current_function = _authority_descriptor_function(current_descriptor)
        if (
            current_descriptor is not expected_descriptor
            or current_function is not expected_function
            or current_function is None
            or current_function.__code__ is not expected_code
        ):
            raise RuntimeError(error)


def _capture_class_transition_graph(
    owner: type,
    roots: tuple[str, ...],
) -> tuple[
    tuple[tuple[str, object, FunctionType, object], ...],
    tuple[tuple[str, dict[str, object], object, object | None], ...],
]:
    """Snapshot transitive class dispatch plus same-module Python dependencies."""

    methods: dict[str, tuple[object, FunctionType, object]] = {}
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in methods:
            continue
        descriptor = owner.__dict__.get(name)
        function = _class_descriptor_function(descriptor)
        if function is None:
            raise RuntimeError("monotonic transition helper graph is incomplete")
        methods[name] = (descriptor, function, function.__code__)
        for candidate in function.__code__.co_names:
            candidate_descriptor = owner.__dict__.get(candidate)
            if _class_descriptor_function(candidate_descriptor) is not None:
                pending.append(candidate)

    globals_witness: dict[
        tuple[int, str], tuple[str, dict[str, object], object, object | None]
    ] = {}
    function_pending = [item[1] for item in methods.values()]
    visited: set[int] = set()
    while function_pending:
        current = function_pending.pop()
        marker = id(current)
        if marker in visited:
            continue
        visited.add(marker)
        namespace = current.__globals__
        for name in current.__code__.co_names:
            if name not in namespace:
                continue
            value = namespace[name]
            if type(value) is not FunctionType or value.__globals__ is not namespace:
                continue
            key = (id(namespace), name)
            if key not in globals_witness:
                globals_witness[key] = (
                    name,
                    namespace,
                    value,
                    value.__code__,
                )
            function_pending.append(value)

    method_witness = tuple(
        (name, methods[name][0], methods[name][1], methods[name][2])
        for name in sorted(methods)
    )
    global_witness = tuple(
        globals_witness[key] for key in sorted(globals_witness)
    )
    return method_witness, global_witness


def _require_class_transition_graph(
    owner: type,
    *,
    methods: tuple[tuple[str, object, FunctionType, object], ...],
    globals_witness: tuple[
        tuple[str, dict[str, object], object, object | None], ...
    ],
    error: str,
) -> None:
    for name, expected_descriptor, expected_function, expected_code in methods:
        current_descriptor = owner.__dict__.get(name)
        current_function = _class_descriptor_function(current_descriptor)
        if (
            current_descriptor is not expected_descriptor
            or current_function is not expected_function
            or current_function is None
            or current_function.__code__ is not expected_code
        ):
            raise RuntimeError(error)
    for name, namespace, expected_value, expected_code in globals_witness:
        if namespace.get(name) is not expected_value:
            raise RuntimeError(error)
        if (
            expected_code is not None
            and (
                type(expected_value) is not FunctionType
                or expected_value.__code__ is not expected_code
            )
        ):
            raise RuntimeError(error)


_MONOTONIC_TRANSITION_METHOD_WITNESS, _MONOTONIC_TRANSITION_GLOBAL_WITNESS = (
    _capture_class_transition_graph(
        MonotonicWorkspaceAuthority,
        ("__init__", "recover", "prepare", "commit"),
    )
)


_TURNOVER_RESOLVE_DESCRIPTOR = PaperDayTurnoverResolver.__dict__["resolve"]
if type(_TURNOVER_RESOLVE_DESCRIPTOR) is not classmethod:
    raise RuntimeError("canonical turnover resolver descriptor is unavailable")
_TURNOVER_RESOLVE_FUNCTION = _TURNOVER_RESOLVE_DESCRIPTOR.__func__
_TURNOVER_RESOLVE_CODE = getattr(_TURNOVER_RESOLVE_FUNCTION, "__code__", None)
if type(_TURNOVER_RESOLVE_FUNCTION) is not FunctionType or _TURNOVER_RESOLVE_CODE is None:
    raise RuntimeError("canonical turnover resolver executable is unavailable")
(
    _TURNOVER_RESOLVE_CLOSURE_WITNESS,
    _TURNOVER_RESOLVE_BINDING_WITNESS,
) = _capture_sealed_wrapper_authority(_TURNOVER_RESOLVE_FUNCTION)

_RISK_DAY_STORE_METHOD_NAMES = (
    "__init__",
    "current",
    "require_current",
    "require_current_under_lock",
    "_current_under_lock",
    "_publish_day",
    "_evidence",
)
_RISK_DAY_STORE_WITNESSES = tuple(
    (
        name,
        ProductDayRiskWindowStore.__dict__[name],
        ProductDayRiskWindowStore.__dict__[name].__code__,
        *_capture_sealed_wrapper_authority(
            ProductDayRiskWindowStore.__dict__[name]
        ),
    )
    for name in _RISK_DAY_STORE_METHOD_NAMES
)


def _require_product_day_turnover_dispatch() -> None:
    """Fail closed if positive day/turnover executable authority drifts."""

    _require_instance_state_class_witnesses(
        ProductDayRiskWindowStore,
        _PRODUCT_DAY_STORE_STATE_WITNESSES,
        error="product day store state authority changed",
    )
    _require_instance_state_class_witnesses(
        MonotonicWorkspaceAuthority,
        _MONOTONIC_AUTHORITY_STATE_WITNESSES,
        error="monotonic day authority state changed",
    )
    _require_class_transition_graph(
        MonotonicWorkspaceAuthority,
        methods=_MONOTONIC_TRANSITION_METHOD_WITNESS,
        globals_witness=_MONOTONIC_TRANSITION_GLOBAL_WITNESS,
        error="monotonic day authority transition graph changed",
    )

    current_resolve_descriptor = PaperDayTurnoverResolver.__dict__.get("resolve")
    if (
        current_resolve_descriptor is not _TURNOVER_RESOLVE_DESCRIPTOR
        or type(current_resolve_descriptor) is not classmethod
        or current_resolve_descriptor.__func__ is not _TURNOVER_RESOLVE_FUNCTION
        or getattr(current_resolve_descriptor.__func__, "__code__", None)
        is not _TURNOVER_RESOLVE_CODE
    ):
        raise RuntimeError("product day turnover resolver executable authority changed")
    _require_sealed_wrapper_authority(
        _TURNOVER_RESOLVE_FUNCTION,
        expected_closure=_TURNOVER_RESOLVE_CLOSURE_WITNESS,
        expected_bindings=_TURNOVER_RESOLVE_BINDING_WITNESS,
        error="product day turnover resolver dependency authority changed",
    )

    for (
        name,
        expected_function,
        expected_code,
        expected_closure,
        expected_bindings,
    ) in _RISK_DAY_STORE_WITNESSES:
        current_function = ProductDayRiskWindowStore.__dict__.get(name)
        if (
            current_function is not expected_function
            or type(current_function) is not FunctionType
            or getattr(current_function, "__code__", None) is not expected_code
        ):
            raise RuntimeError("product day window executable authority changed")
        _require_sealed_wrapper_authority(
            current_function,
            expected_closure=expected_closure,
            expected_bindings=expected_bindings,
            error="product day window dependency authority changed",
        )


def _canonical_workspace_root(workspace: str | Path) -> Path:
    """Resolve the economic workspace through the frozen canonical Path surface."""

    path_witnesses = (
        (Path.__new__, _ADMISSION_PATH_NEW, _ADMISSION_PATH_NEW_CODE),
        (Path.__init__, _ADMISSION_PATH_INIT, _ADMISSION_PATH_INIT_CODE),
        (
            Path.expanduser,
            _ADMISSION_PATH_EXPANDUSER,
            _ADMISSION_PATH_EXPANDUSER_CODE,
        ),
        (Path.resolve, _ADMISSION_PATH_RESOLVE, _ADMISSION_PATH_RESOLVE_CODE),
        (Path.__truediv__, _ADMISSION_PATH_TRUEDIV, _ADMISSION_PATH_TRUEDIV_CODE),
        (Path.lstat, _ADMISSION_PATH_LSTAT, _ADMISSION_PATH_LSTAT_CODE),
        (Path.iterdir, _ADMISSION_PATH_ITERDIR, _ADMISSION_PATH_ITERDIR_CODE),
        (Path.exists, _ADMISSION_PATH_EXISTS, _ADMISSION_PATH_EXISTS_CODE),
    )
    for current, expected, expected_code in path_witnesses:
        if (
            current is not expected
            or getattr(current, "__code__", None) is not expected_code
        ):
            raise RuntimeError("economic admission workspace path authority changed")

    candidate = Path(workspace)
    if type(candidate) is not _ADMISSION_CANONICAL_PATH_TYPE:
        raise RuntimeError("economic admission workspace path type is not canonical")
    expanded = _ADMISSION_PATH_EXPANDUSER(candidate)
    if type(expanded) is not _ADMISSION_CANONICAL_PATH_TYPE:
        raise RuntimeError("economic admission workspace path type is not canonical")
    resolved = _ADMISSION_PATH_RESOLVE(expanded, strict=False)
    if type(resolved) is not _ADMISSION_CANONICAL_PATH_TYPE:
        raise RuntimeError("economic admission workspace path type is not canonical")
    return resolved


@dataclass(frozen=True, slots=True)
class PaperAdmissionResult:
    """Result of one risk-evaluate + PAPER ticket-open critical section."""

    risk: RiskDecision
    ticket: PaperTicket | None
    book: PaperBook

    @property
    def admitted(self) -> bool:
        return self.ticket is not None


@dataclass(frozen=True, slots=True)
class _PaperDayTurnoverSnapshot:
    """Read-only pre-lock evidence that must be revalidated under the writer lock."""

    book: PaperBook
    evidence: PaperDayTurnoverEvidence
    window_evidence: ProductDayRiskWindow


@dataclass(frozen=True, slots=True)
class _ProductDayAdmissionAuthority:
    """Current product-day headroom plus durable product-day admission identity."""

    turnover_room: Decimal
    admission_ts: str
    day_key: str
    window_start: str
    window_end_exclusive: str
    window_state_sha256: str
    window_authority_generation: int
    window_store: ProductDayRiskWindowStore
    window_evidence: ProductDayRiskWindow


_DAY_AUTHORITY_FIELD_DESCRIPTOR_WITNESSES = tuple(
    (
        owner,
        tuple(
            (name, owner.__dict__[name])
            for name in tuple(owner.__dataclass_fields__)
        ),
    )
    for owner in (
        _PaperDayTurnoverSnapshot,
        _ProductDayAdmissionAuthority,
        PaperDayTurnoverEvidence,
        ProductDayRiskWindow,
        AuthorityRecovery,
    )
)


_DAY_AUTHORITY_EXECUTABLE_DESCRIPTOR_WITNESSES = (
    (
        _PaperDayTurnoverSnapshot,
        _capture_executable_descriptor_witnesses(
            _PaperDayTurnoverSnapshot,
            ("__init__",),
        ),
    ),
    (
        _ProductDayAdmissionAuthority,
        _capture_executable_descriptor_witnesses(
            _ProductDayAdmissionAuthority,
            ("__init__",),
        ),
    ),
    (
        PaperDayTurnoverEvidence,
        _capture_executable_descriptor_witnesses(
            PaperDayTurnoverEvidence,
            ("__init__", "__post_init__"),
        ),
    ),
    (
        ProductDayRiskWindow,
        _capture_executable_descriptor_witnesses(
            ProductDayRiskWindow,
            ("__init__", "__post_init__", "__eq__"),
        ),
    ),
    (
        AuthorityRecovery,
        _capture_executable_descriptor_witnesses(
            AuthorityRecovery,
            ("__init__",),
        ),
    ),
)


def _require_day_authority_data_descriptors() -> None:
    """Reject class-level rewrites of positive day/turnover evidence authority."""

    for owner, witnesses in _DAY_AUTHORITY_FIELD_DESCRIPTOR_WITNESSES:
        for name, expected_descriptor in witnesses:
            if owner.__dict__.get(name) is not expected_descriptor:
                raise RuntimeError(
                    "economic admission day authority data descriptor changed"
                )
    for owner, witnesses in _DAY_AUTHORITY_EXECUTABLE_DESCRIPTOR_WITNESSES:
        _require_executable_descriptor_witnesses(
            owner,
            witnesses,
            error="economic admission day authority executable descriptor changed",
        )


def _canonical_day_authority_field(
    instance: object,
    owner: type,
    name: str,
) -> object:
    """Read one day-authority slot through its captured canonical descriptor."""

    _require_day_authority_data_descriptors()
    if type(instance) is not owner:
        raise RuntimeError("economic admission day authority type changed")
    for witness_owner, witnesses in _DAY_AUTHORITY_FIELD_DESCRIPTOR_WITNESSES:
        if witness_owner is not owner:
            continue
        for field_name, descriptor in witnesses:
            if field_name == name:
                return descriptor.__get__(instance, owner)
        break
    raise RuntimeError("economic admission day authority field is unavailable")


def _positive_decimal(value: Decimal | str) -> Decimal:
    if type(value) is Decimal:
        amount = value
    elif type(value) is str:
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("stake must be a finite positive decimal") from exc
    else:
        raise ValueError("stake must be a finite positive decimal")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("stake must be a finite positive decimal")
    return amount


def _same_semantic_book_state(expected: PaperBook, observed: PaperBook) -> bool:
    """Compare the complete validated economic state published by one admission."""

    PaperBook._validate_loaded_state(expected)
    PaperBook._validate_loaded_state(observed)
    return (
        observed.initial_bankroll == expected.initial_bankroll
        and observed.balance == expected.balance
        and observed.tickets == expected.tickets
        and observed._lifecycle == expected._lifecycle
        and observed._settlement_times == expected._settlement_times
        and observed._product_day_admissions == expected._product_day_admissions
    )


def _validate_context_binding(
    *,
    context: ProposedTicketRiskContext | None,
    legs: tuple[TicketLeg, ...],
    placed_at: str,
    provider_source_ids: tuple[str, ...],
    provider_accounts: tuple[tuple[str, str], ...],
    bankroll_id: str | None,
    currency: str | None,
) -> None:
    """Bind the risk-reviewed proposal to the exact ticket that will be opened."""

    if context is None:
        return
    if context.legs != legs:
        raise ValueError("risk context legs must match admitted ticket legs")
    if context.proposal_ts is not None and context.proposal_ts != placed_at:
        raise ValueError(
            "risk context proposal_ts must match admitted ticket placed_at"
        )
    if context.provider_accounts != provider_accounts:
        raise ValueError(
            "risk context provider accounts must match admitted ticket provenance"
        )
    if context.bankroll_id != bankroll_id or context.currency != currency:
        raise ValueError(
            "risk context bankroll and currency must match admitted ticket provenance"
        )
    if context.quotes:
        quote_source_ids = tuple(sorted({quote.source_id for quote in context.quotes}))
        if quote_source_ids != provider_source_ids:
            raise ValueError(
                "risk context quote sources must match admitted ticket provider sources"
            )


def _parse_utc_timestamp(value: str) -> datetime | None:
    if type(value) is not str or not value or value != value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _product_clock_admission_timestamp(
    *,
    window_store: ProductDayRiskWindowStore,
    current_window: ProductDayRiskWindow,
) -> str | None:
    """Issue the admission instant from the same product clock as current day authority."""

    if not _canonical_day_authority_field(
        current_window,
        ProductDayRiskWindow,
        "product_clock_authoritative",
    ):
        return None
    clock = window_store._clock
    try:
        raw_ns = clock()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if type(raw_ns) is not int or raw_ns < 0:
        return None
    seconds, nanoseconds = divmod(raw_ns, 1_000_000_000)
    try:
        instant = datetime.fromtimestamp(seconds, timezone.utc).replace(
            microsecond=nanoseconds // 1_000,
        )
    except (OverflowError, OSError, ValueError):
        return None
    window_start = _parse_utc_timestamp(
        _canonical_day_authority_field(
            current_window,
            ProductDayRiskWindow,
            "window_start",
        )
    )
    window_end = _parse_utc_timestamp(
        _canonical_day_authority_field(
            current_window,
            ProductDayRiskWindow,
            "window_end_exclusive",
        )
    )
    if (
        window_start is None
        or window_end is None
        or not (window_start <= instant < window_end)
    ):
        return None
    return instant.isoformat().replace("+00:00", "Z")


def _quote_context_with_product_admission_time(
    context: ProposedTicketRiskContext,
    admission_ts: str,
) -> ProposedTicketRiskContext:
    """Build quote-only context using product time without rewriting other evidence."""

    return ProposedTicketRiskContext(
        legs=context.legs,
        quotes=context.quotes,
        provider_accounts=context.provider_accounts,
        bankroll_id=context.bankroll_id,
        currency=context.currency,
        proposal_ts=admission_ts,
    )


def _canonical_economic_goal_store(root: Path) -> EconomicGoalStore:
    """Construct the exact durable goal store and reject constructor/path drift."""

    _require_instance_state_class_witnesses(
        EconomicGoalStore,
        _ECONOMIC_GOAL_STORE_STATE_WITNESSES,
        error="economic goal store state authority changed",
    )
    if (
        EconomicGoalStore.__dict__.get("FILE_NAME") != _ECONOMIC_GOAL_FILE_NAME
        or EconomicGoalStore.__new__ is not _ECONOMIC_GOAL_STORE_NEW
        or getattr(EconomicGoalStore.__new__, "__code__", None)
        is not _ECONOMIC_GOAL_STORE_NEW_CODE
        or EconomicGoalStore.__init__ is not _ECONOMIC_GOAL_STORE_INIT
        or getattr(EconomicGoalStore.__init__, "__code__", None)
        is not _ECONOMIC_GOAL_STORE_INIT_CODE
    ):
        raise RuntimeError("economic goal store authority changed")
    if type(root) is not _ADMISSION_CANONICAL_PATH_TYPE:
        raise RuntimeError("economic goal store path is not canonical")
    store = EconomicGoalStore(root)
    if type(store) is not EconomicGoalStore:
        raise RuntimeError("economic goal store authority changed")
    if type(store.workspace) is not _ADMISSION_CANONICAL_PATH_TYPE:
        raise RuntimeError("economic goal store path is not canonical")
    store_workspace = _ADMISSION_PATH_RESOLVE(
        _ADMISSION_PATH_EXPANDUSER(store.workspace),
        strict=False,
    )
    expected_path = _ADMISSION_PATH_TRUEDIV(root, _ECONOMIC_GOAL_FILE_NAME)
    if (
        type(store_workspace) is not _ADMISSION_CANONICAL_PATH_TYPE
        or type(store.path) is not _ADMISSION_CANONICAL_PATH_TYPE
        or store_workspace != root
        or store.path != expected_path
    ):
        raise RuntimeError("economic goal store path is not canonical")
    return store


def _prepare_paper_day_turnover_snapshot(
    *,
    root: Path,
    book_path: Path,
    risk_policy: PaperRiskPolicy,
) -> _PaperDayTurnoverSnapshot | None:
    """Resolve product-issued day turnover before the admission lock is acquired.

    ProductDayRiskWindowStore itself owns the canonical workspace lock while it
    resolves/advances the UTC-day authority. Admission therefore resolves a
    read-only snapshot first and later revalidates every mutable dependency while
    holding its own economic writer lock. Any intervening change disables the
    bounded-day override and falls back to the conservative whole-history policy.
    """

    goal = risk_policy.economic_goal
    if goal is None or not _ADMISSION_PATH_EXISTS(book_path):
        return None
    _require_day_authority_data_descriptors()
    _require_product_day_turnover_dispatch()
    try:
        goal_store = _canonical_economic_goal_store(root)
        if _ECONOMIC_GOAL_LOAD_FROZEN(goal_store) != goal:
            return None
        _require_paperbook_admission_authority()
        snapshot_book = _PAPERBOOK_LOAD_FUNCTION(PaperBook, book_path)
        window_store = ProductDayRiskWindowStore(root)
        window = window_store.current()
        if not _canonical_day_authority_field(
            window,
            ProductDayRiskWindow,
            "product_clock_authoritative",
        ):
            return None
        evidence = PaperDayTurnoverResolver.resolve(
            book=snapshot_book,
            goal_store=goal_store,
            window_store=window_store,
            window_evidence=window,
        )
        _require_product_day_turnover_dispatch()
        _require_day_authority_data_descriptors()
    except (ArithmeticError, OSError, RuntimeError, TypeError, ValueError):
        return None
    return _PaperDayTurnoverSnapshot(
        book=snapshot_book,
        evidence=evidence,
        window_evidence=window,
    )


def _revalidated_product_day_admission_authority(
    *,
    snapshot: _PaperDayTurnoverSnapshot | None,
    root: Path,
    book: PaperBook,
    risk_policy: PaperRiskPolicy,
    placed_at: str,
    workspace_lock: WorkspaceEconomicLock,
) -> _ProductDayAdmissionAuthority | None:
    """Return current day room plus a product-owned action instant under one lock."""

    if snapshot is None:
        return None
    _require_day_authority_data_descriptors()
    goal = risk_policy.economic_goal
    if goal is None:
        return None
    try:
        _require_product_day_turnover_dispatch()
        snapshot_book = _canonical_day_authority_field(
            snapshot,
            _PaperDayTurnoverSnapshot,
            "book",
        )
        snapshot_window_evidence = _canonical_day_authority_field(
            snapshot,
            _PaperDayTurnoverSnapshot,
            "window_evidence",
        )
        evidence = _canonical_day_authority_field(
            snapshot,
            _PaperDayTurnoverSnapshot,
            "evidence",
        )
        if not _same_semantic_book_state(snapshot_book, book):
            return None
        goal_store = _canonical_economic_goal_store(root)
        durable_goal = _ECONOMIC_GOAL_LOAD_FROZEN(goal_store)
        if durable_goal != goal:
            return None

        # Re-resolve the product clock + independent monotonic generation while the
        # exact economic writer lock is already held. Raw risk-day bytes or wall-clock
        # equality are not positive authority and cannot substitute for this check.
        window_store = ProductDayRiskWindowStore(root)
        current_window = ProductDayRiskWindowStore.require_current_under_lock(
            window_store,
            snapshot_window_evidence,
            workspace_lock=workspace_lock,
        )
        _require_product_day_turnover_dispatch()
        _require_day_authority_data_descriptors()

        provenance = provenance_for(goal)
        evidence_goal_id = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "goal_id"
        )
        evidence_goal_revision = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "goal_revision"
        )
        evidence_goal_sha = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "goal_contract_sha256"
        )
        evidence_bankroll_id = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "bankroll_id"
        )
        evidence_currency = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "currency"
        )
        evidence_initial_bankroll = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "initial_bankroll"
        )
        evidence_day_key = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "day_key"
        )
        evidence_window_start = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "window_start"
        )
        evidence_window_end = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "window_end_exclusive"
        )
        evidence_state_sha = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "window_state_sha256"
        )
        evidence_generation = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "window_authority_generation"
        )
        evidence_breached = _canonical_day_authority_field(
            evidence, PaperDayTurnoverEvidence, "breached"
        )
        current_day_key = _canonical_day_authority_field(
            current_window, ProductDayRiskWindow, "day_key"
        )
        current_window_start = _canonical_day_authority_field(
            current_window, ProductDayRiskWindow, "window_start"
        )
        current_window_end = _canonical_day_authority_field(
            current_window, ProductDayRiskWindow, "window_end_exclusive"
        )
        current_state_sha = _canonical_day_authority_field(
            current_window, ProductDayRiskWindow, "state_sha256"
        )
        current_generation = _canonical_day_authority_field(
            current_window, ProductDayRiskWindow, "authority_generation"
        )
        current_clock_authoritative = _canonical_day_authority_field(
            current_window, ProductDayRiskWindow, "product_clock_authoritative"
        )
        if (
            evidence_goal_id != goal.goal_id
            or evidence_goal_revision != goal.revision
            or evidence_goal_sha != provenance.contract_sha256
            or evidence_bankroll_id != goal.bankroll_id
            or evidence_currency != goal.currency
            or evidence_initial_bankroll != book.initial_bankroll
            or evidence_day_key != current_day_key
            or evidence_window_start != current_window_start
            or evidence_window_end != current_window_end
            or evidence_state_sha != current_state_sha
            or evidence_generation != current_generation
            or not current_clock_authoritative
            or evidence_breached
        ):
            return None

        candidate_time = _parse_utc_timestamp(placed_at)
        window_start = _parse_utc_timestamp(evidence_window_start)
        window_end = _parse_utc_timestamp(evidence_window_end)
        if (
            candidate_time is None
            or window_start is None
            or window_end is None
            or not (window_start <= candidate_time < window_end)
        ):
            return None
        room = _canonical_day_authority_field(
            evidence,
            PaperDayTurnoverEvidence,
            "residual_headroom",
        )
        if type(room) is not Decimal or not room.is_finite() or room < 0:
            return None
        admission_ts = _product_clock_admission_timestamp(
            window_store=window_store,
            current_window=current_window,
        )
        if admission_ts is None:
            return None
        admission_time = _parse_utc_timestamp(admission_ts)
        if admission_time is None or admission_time < candidate_time:
            return None
        _require_product_day_turnover_dispatch()
        return _ProductDayAdmissionAuthority(
            turnover_room=room,
            admission_ts=admission_ts,
            day_key=current_day_key,
            window_start=current_window_start,
            window_end_exclusive=current_window_end,
            window_state_sha256=current_state_sha,
            window_authority_generation=current_generation,
            window_store=window_store,
            window_evidence=current_window,
        )
    except (ArithmeticError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _freeze_external_python_function_graph(function: FunctionType) -> FunctionType:
    """Detach one Python function and its same-module helper graph from rebinding."""

    if type(function) is not FunctionType or function.__closure__ is not None:
        raise RuntimeError("canonical risk helper must be a closure-free Python function")
    source_globals = function.__globals__
    frozen_globals: dict[str, object] = dict(source_globals)
    clones: dict[str, FunctionType] = {}
    for name, value in tuple(source_globals.items()):
        if (
            type(value) is FunctionType
            and value.__globals__ is source_globals
            and value.__closure__ is None
        ):
            clone = FunctionType(
                value.__code__,
                frozen_globals,
                name=value.__name__,
                argdefs=value.__defaults__,
            )
            if value.__kwdefaults__ is not None:
                clone.__kwdefaults__ = dict(value.__kwdefaults__)
            clone.__annotations__ = dict(value.__annotations__)
            clone.__qualname__ = value.__qualname__
            clone.__doc__ = value.__doc__
            clones[name] = clone
    frozen_globals.update(clones)
    frozen = FunctionType(
        function.__code__,
        frozen_globals,
        name=function.__name__,
        argdefs=function.__defaults__,
    )
    if function.__kwdefaults__ is not None:
        frozen.__kwdefaults__ = dict(function.__kwdefaults__)
    frozen.__annotations__ = dict(function.__annotations__)
    frozen.__qualname__ = function.__qualname__
    frozen.__doc__ = function.__doc__
    return frozen


# Durable goal truth is part of the positive under-lock admission boundary. Keep the
# exact load implementation and all same-module parsing helpers detached from later
# EconomicGoalStore method/global rebinding, just like the risk suffix below.
_ECONOMIC_GOAL_LOAD_FROZEN = _freeze_external_python_function_graph(
    EconomicGoalStore.__dict__["load"]
)


# Freeze the exact canonical risk helper descriptors used by the positive
# post-turnover continuation. This mirrors PaperRiskPolicy.evaluate's own helper
# witnesses so a later class/descriptor mutation cannot widen admission authority.
_RISK_EVALUATE_DESCRIPTOR = PaperRiskPolicy.__dict__["evaluate"]
_RISK_EVALUATE = _RISK_EVALUATE_DESCRIPTOR
_RISK_EVALUATE_FROZEN = _freeze_external_python_function_graph(_RISK_EVALUATE)

_RISK_BOOK_STATE_DESCRIPTOR = PaperRiskPolicy.__dict__["_book_state"]
_RISK_HISTORY_DESCRIPTOR = PaperRiskPolicy.__dict__["_goal_history_rooms"]
_RISK_QUOTE_DESCRIPTOR = PaperRiskPolicy.__dict__["_quote_risk_decision"]
_RISK_RUIN_DESCRIPTOR = PaperRiskPolicy.__dict__["_risk_of_ruin_evidence_decision"]
_RISK_DERIVED_DESCRIPTOR = PaperRiskPolicy.__dict__["_derived_risk_values"]

_RISK_BOOK_STATE = _RISK_BOOK_STATE_DESCRIPTOR.__func__
_RISK_HISTORY = _RISK_HISTORY_DESCRIPTOR.__func__
_RISK_QUOTE = _RISK_QUOTE_DESCRIPTOR.__func__
_RISK_RUIN = _RISK_RUIN_DESCRIPTOR.__func__
_RISK_DERIVED = _RISK_DERIVED_DESCRIPTOR

_RISK_HISTORY_FROZEN = _freeze_external_python_function_graph(_RISK_HISTORY)
_RISK_QUOTE_FROZEN = _freeze_external_python_function_graph(_RISK_QUOTE)
_RISK_RUIN_FROZEN = _freeze_external_python_function_graph(_RISK_RUIN)
_RISK_DERIVED_FROZEN = _freeze_external_python_function_graph(_RISK_DERIVED)

_ADMISSION_RISK_HELPER_WITNESSES = (
    ("evaluate", _RISK_EVALUATE_DESCRIPTOR, _RISK_EVALUATE, _RISK_EVALUATE.__code__, False),
    ("_book_state", _RISK_BOOK_STATE_DESCRIPTOR, _RISK_BOOK_STATE, _RISK_BOOK_STATE.__code__, True),
    ("_goal_history_rooms", _RISK_HISTORY_DESCRIPTOR, _RISK_HISTORY, _RISK_HISTORY.__code__, True),
    ("_quote_risk_decision", _RISK_QUOTE_DESCRIPTOR, _RISK_QUOTE, _RISK_QUOTE.__code__, True),
    ("_risk_of_ruin_evidence_decision", _RISK_RUIN_DESCRIPTOR, _RISK_RUIN, _RISK_RUIN.__code__, True),
    ("_derived_risk_values", _RISK_DERIVED_DESCRIPTOR, _RISK_DERIVED, _RISK_DERIVED.__code__, False),
)


def _capture_detached_function_graph(
    function: FunctionType,
) -> tuple[
    object,
    tuple[tuple[str, dict[str, object], object, object | None], ...],
]:
    """Witness one detached function plus its recursively used frozen namespace."""

    if type(function) is not FunctionType:
        raise RuntimeError("detached risk helper must be a Python function")
    namespace = function.__globals__
    pending = [function]
    visited: set[int] = set()
    bindings: dict[
        tuple[int, str], tuple[str, dict[str, object], object, object | None]
    ] = {}
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in visited:
            continue
        visited.add(marker)
        if current.__globals__ is not namespace:
            raise RuntimeError("detached risk helper namespace changed")
        for name in current.__code__.co_names:
            if name not in namespace:
                continue
            value = namespace[name]
            key = (id(namespace), name)
            if key not in bindings:
                bindings[key] = (
                    name,
                    namespace,
                    value,
                    value.__code__ if type(value) is FunctionType else None,
                )
            if type(value) is FunctionType and value.__globals__ is namespace:
                pending.append(value)
    return function.__code__, tuple(bindings[key] for key in sorted(bindings))


_RISK_DECISION_TYPE = RiskDecision
_RISK_DECISION_ALLOWED_DESCRIPTOR = RiskDecision.__dict__["allowed"]
_RISK_DECISION_REASON_DESCRIPTOR = RiskDecision.__dict__["reason"]


_RISK_DECISION_EXECUTABLE_WITNESSES = _capture_executable_descriptor_witnesses(
    RiskDecision,
    ("__init__",),
)


def _require_risk_decision_authority() -> None:
    """Bind rejection/approval routing to exact canonical decision authority."""

    _require_executable_descriptor_witnesses(
        RiskDecision,
        _RISK_DECISION_EXECUTABLE_WITNESSES,
        error="economic admission risk decision executable authority changed",
    )
    if (
        RiskDecision is not _RISK_DECISION_TYPE
        or _RISK_DECISION_TYPE.__dict__.get("allowed")
        is not _RISK_DECISION_ALLOWED_DESCRIPTOR
        or _RISK_DECISION_TYPE.__dict__.get("reason")
        is not _RISK_DECISION_REASON_DESCRIPTOR
    ):
        raise RuntimeError("economic admission risk decision authority changed")


def _canonical_risk_decision_state(
    decision: RiskDecision,
) -> tuple[bool, str]:
    _require_risk_decision_authority()
    if type(decision) is not _RISK_DECISION_TYPE:
        raise RuntimeError("economic admission risk decision type changed")
    allowed = _RISK_DECISION_ALLOWED_DESCRIPTOR.__get__(
        decision,
        _RISK_DECISION_TYPE,
    )
    reason = _RISK_DECISION_REASON_DESCRIPTOR.__get__(
        decision,
        _RISK_DECISION_TYPE,
    )
    if type(allowed) is not bool or type(reason) is not str or not reason:
        raise RuntimeError("economic admission risk decision state is invalid")
    return allowed, reason


_RISK_POLICY_FIELD_DESCRIPTOR_WITNESSES = tuple(
    (name, PaperRiskPolicy.__dict__[name])
    for name in tuple(PaperRiskPolicy.__dataclass_fields__)
)
_PROPOSED_CONTEXT_FIELD_DESCRIPTOR_WITNESSES = tuple(
    (name, ProposedTicketRiskContext.__dict__[name])
    for name in tuple(ProposedTicketRiskContext.__dataclass_fields__)
)
_RISK_OF_RUIN_EVIDENCE_FIELD_DESCRIPTOR_WITNESSES = tuple(
    (name, RiskOfRuinEvidence.__dict__[name])
    for name in tuple(RiskOfRuinEvidence.__dataclass_fields__)
)
_TICKET_LEG_FIELD_DESCRIPTOR_WITNESSES = tuple(
    (name, TicketLeg.__dict__[name])
    for name in tuple(TicketLeg.__dataclass_fields__)
)
_MARKET_EVENT_FIELD_DESCRIPTOR_WITNESSES = tuple(
    (name, MarketEvent.__dict__[name])
    for name in tuple(MarketEvent.__dataclass_fields__)
)
_PAPER_TICKET_FIELD_DESCRIPTOR_WITNESSES = tuple(
    (name, PaperTicket.__dict__[name])
    for name in tuple(PaperTicket.__dataclass_fields__)
)
_ECONOMIC_GOAL_FIELD_DESCRIPTOR_WITNESSES = tuple(
    (name, EconomicGoalContract.__dict__[name])
    for name in tuple(EconomicGoalContract.__dataclass_fields__)
)


_PROPOSED_CONTEXT_EXECUTABLE_WITNESSES = _capture_executable_descriptor_witnesses(
    ProposedTicketRiskContext,
    ("__init__", "__post_init__"),
)
_TICKET_LEG_EXECUTABLE_WITNESSES = _capture_executable_descriptor_witnesses(
    TicketLeg,
    ("__eq__", "quote_key"),
)
_MARKET_EVENT_EXECUTABLE_WITNESSES = _capture_executable_descriptor_witnesses(
    MarketEvent,
    ("__eq__", "quote_key", "to_dict", "from_dict"),
)
_ECONOMIC_GOAL_EXECUTABLE_WITNESSES = _capture_executable_descriptor_witnesses(
    EconomicGoalContract,
    ("__init__", "__post_init__", "__eq__"),
)
_PAPER_TICKET_EXECUTABLE_WITNESSES = _capture_executable_descriptor_witnesses(
    PaperTicket,
    ("__init__", "__eq__"),
)


_DETACHED_RISK_HELPER_WITNESSES = tuple(
    (
        label,
        function,
        *_capture_detached_function_graph(function),
    )
    for label, function in (
        ("evaluate", _RISK_EVALUATE_FROZEN),
        ("history", _RISK_HISTORY_FROZEN),
        ("quote", _RISK_QUOTE_FROZEN),
        ("ruin", _RISK_RUIN_FROZEN),
        ("derived", _RISK_DERIVED_FROZEN),
    )
)

_RISK_POLICY_TRANSITION_METHOD_WITNESS, _RISK_POLICY_TRANSITION_GLOBAL_WITNESS = (
    _capture_class_transition_graph(
        PaperRiskPolicy,
        (
            "_book_state",
            "_goal_history_rooms",
            "_quote_risk_decision",
            "_risk_of_ruin_evidence_decision",
            "_derived_risk_values",
        ),
    )
)


_PAPERBOOK_INSTANCE_STATE_CLASS_WITNESSES = tuple(
    (
        name,
        name in PaperBook.__dict__,
        PaperBook.__dict__.get(name),
    )
    for name in (
        "initial_bankroll",
        "balance",
        "tickets",
        "_lifecycle",
        "_settlement_times",
        "_product_day_admissions",
    )
)

_PAPERBOOK_LOAD_DESCRIPTOR = PaperBook.__dict__["load"]
if type(_PAPERBOOK_LOAD_DESCRIPTOR) is not classmethod:
    raise RuntimeError("canonical PaperBook load authority is unavailable")
_PAPERBOOK_LOAD_FUNCTION = _PAPERBOOK_LOAD_DESCRIPTOR.__func__
_PAPERBOOK_SAVE_FUNCTION = PaperBook.__dict__["save"]
_PAPERBOOK_OPEN_TICKET_FUNCTION = PaperBook.__dict__["open_ticket"]
_PAPERBOOK_PREPARE_PRODUCT_DAY_ADMISSION_FUNCTION = PaperBook.__dict__[
    "_prepare_product_day_admission"
]
_PAPERBOOK_RECORD_PRODUCT_DAY_ADMISSION_FUNCTION = PaperBook.__dict__[
    "_record_product_day_admission"
]
if (
    type(_PAPERBOOK_LOAD_FUNCTION) is not FunctionType
    or type(_PAPERBOOK_SAVE_FUNCTION) is not FunctionType
    or type(_PAPERBOOK_OPEN_TICKET_FUNCTION) is not FunctionType
    or type(_PAPERBOOK_PREPARE_PRODUCT_DAY_ADMISSION_FUNCTION) is not FunctionType
    or type(_PAPERBOOK_RECORD_PRODUCT_DAY_ADMISSION_FUNCTION) is not FunctionType
):
    raise RuntimeError("canonical PaperBook admission mutation authority is unavailable")
_PAPERBOOK_GATE_METHOD_WITNESS, _PAPERBOOK_GATE_GLOBAL_WITNESS = (
    _capture_class_transition_graph(
        PaperBook,
        (
            "load",
            "save",
            "open_ticket",
            "_prepare_product_day_admission",
            "_record_product_day_admission",
        ),
    )
)


def _require_paperbook_admission_authority() -> None:
    """Bind approved PAPER economics to the exact durable mutation graph."""

    for name, expected_present, expected_value in (
        _PAPERBOOK_INSTANCE_STATE_CLASS_WITNESSES
    ):
        if (
            (name in PaperBook.__dict__) is not expected_present
            or PaperBook.__dict__.get(name) is not expected_value
        ):
            raise RuntimeError(
                "economic admission PaperBook state descriptor authority changed"
            )

    for name, expected_descriptor in _PAPER_TICKET_FIELD_DESCRIPTOR_WITNESSES:
        if PaperTicket.__dict__.get(name) is not expected_descriptor:
            raise RuntimeError(
                "economic admission PaperTicket data authority changed"
            )
    _require_executable_descriptor_witnesses(
        PaperTicket,
        _PAPER_TICKET_EXECUTABLE_WITNESSES,
        error="economic admission PaperTicket executable authority changed",
    )

    _require_class_transition_graph(
        PaperBook,
        methods=_PAPERBOOK_GATE_METHOD_WITNESS,
        globals_witness=_PAPERBOOK_GATE_GLOBAL_WITNESS,
        error="economic admission PaperBook mutation authority changed",
    )
    load_descriptor = PaperBook.__dict__.get("load")
    if (
        load_descriptor is not _PAPERBOOK_LOAD_DESCRIPTOR
        or type(load_descriptor) is not classmethod
        or load_descriptor.__func__ is not _PAPERBOOK_LOAD_FUNCTION
        or PaperBook.__dict__.get("save") is not _PAPERBOOK_SAVE_FUNCTION
        or PaperBook.__dict__.get("open_ticket")
        is not _PAPERBOOK_OPEN_TICKET_FUNCTION
        or PaperBook.__dict__.get("_prepare_product_day_admission")
        is not _PAPERBOOK_PREPARE_PRODUCT_DAY_ADMISSION_FUNCTION
        or PaperBook.__dict__.get("_record_product_day_admission")
        is not _PAPERBOOK_RECORD_PRODUCT_DAY_ADMISSION_FUNCTION
    ):
        raise RuntimeError("economic admission PaperBook mutation authority changed")


_RUN_REGISTRY_INIT_FUNCTION = RunRegistry.__dict__["__init__"]
_RUN_REGISTRY_IN_PROGRESS_FUNCTION = RunRegistry.__dict__["in_progress"]
if (
    type(_RUN_REGISTRY_INIT_FUNCTION) is not FunctionType
    or type(_RUN_REGISTRY_IN_PROGRESS_FUNCTION) is not FunctionType
):
    raise RuntimeError("canonical run registry admission gate is unavailable")
_RUN_REGISTRY_GATE_METHOD_WITNESS, _RUN_REGISTRY_GATE_GLOBAL_WITNESS = (
    _capture_class_transition_graph(
        RunRegistry,
        ("__init__", "in_progress"),
    )
)
_RUN_TRANSACTION_GATE_METHOD_WITNESS, _RUN_TRANSACTION_GATE_GLOBAL_WITNESS = (
    _capture_class_transition_graph(
        RunTransaction,
        ("__init__", "_read_manifest", "_validate_manifest_paths"),
    )
)
_RUN_TRANSACTION_ROOT_NAME = RunTransaction.ROOT_NAME
_TRANSACTION_RECOVERY_FUNCTION = transaction_history_requires_recovery
_TRANSACTION_RECOVERY_CODE, _TRANSACTION_RECOVERY_BINDINGS = (
    _capture_detached_function_graph(_TRANSACTION_RECOVERY_FUNCTION)
)


def _require_admission_recovery_gate_authority() -> None:
    """Keep unresolved-run and crash-recovery blockers on canonical dispatch."""

    _require_instance_state_class_witnesses(
        RunRegistry,
        _RUN_REGISTRY_STATE_WITNESSES,
        error="economic admission run registry state authority changed",
    )
    _require_instance_state_class_witnesses(
        RunTransaction,
        _RUN_TRANSACTION_STATE_WITNESSES,
        error="economic admission transaction state authority changed",
    )
    if (
        transaction_history_requires_recovery is not _TRANSACTION_RECOVERY_FUNCTION
        or type(_TRANSACTION_RECOVERY_FUNCTION) is not FunctionType
        or _TRANSACTION_RECOVERY_FUNCTION.__code__ is not _TRANSACTION_RECOVERY_CODE
        or RunTransaction.ROOT_NAME != _RUN_TRANSACTION_ROOT_NAME
    ):
        raise RuntimeError("economic admission recovery gate authority changed")

    for name, namespace, expected_value, expected_code in (
        _TRANSACTION_RECOVERY_BINDINGS
    ):
        if namespace.get(name) is not expected_value:
            raise RuntimeError("economic admission recovery gate authority changed")
        if expected_code is not None and (
            type(expected_value) is not FunctionType
            or expected_value.__code__ is not expected_code
        ):
            raise RuntimeError("economic admission recovery gate authority changed")

    if (
        RunRegistry.__dict__.get("__init__") is not _RUN_REGISTRY_INIT_FUNCTION
        or RunRegistry.__dict__.get("in_progress")
        is not _RUN_REGISTRY_IN_PROGRESS_FUNCTION
    ):
        raise RuntimeError("economic admission run registry gate authority changed")
    _require_class_transition_graph(
        RunRegistry,
        methods=_RUN_REGISTRY_GATE_METHOD_WITNESS,
        globals_witness=_RUN_REGISTRY_GATE_GLOBAL_WITNESS,
        error="economic admission run registry gate authority changed",
    )
    _require_class_transition_graph(
        RunTransaction,
        methods=_RUN_TRANSACTION_GATE_METHOD_WITNESS,
        globals_witness=_RUN_TRANSACTION_GATE_GLOBAL_WITNESS,
        error="economic admission transaction gate authority changed",
    )

    path_witnesses = (
        (Path.__new__, _ADMISSION_PATH_NEW, _ADMISSION_PATH_NEW_CODE),
        (Path.__init__, _ADMISSION_PATH_INIT, _ADMISSION_PATH_INIT_CODE),
        (Path.__truediv__, _ADMISSION_PATH_TRUEDIV, _ADMISSION_PATH_TRUEDIV_CODE),
        (Path.lstat, _ADMISSION_PATH_LSTAT, _ADMISSION_PATH_LSTAT_CODE),
        (Path.iterdir, _ADMISSION_PATH_ITERDIR, _ADMISSION_PATH_ITERDIR_CODE),
    )
    for current, expected, expected_code in path_witnesses:
        if (
            current is not expected
            or getattr(current, "__code__", None) is not expected_code
        ):
            raise RuntimeError("economic admission recovery path authority changed")


def _admission_risk_helper_authority_valid() -> bool:
    """Reject mutation of live or detached positive risk helper authority."""

    for owner, witnesses in (
        (PaperRiskPolicy, _RISK_POLICY_FIELD_DESCRIPTOR_WITNESSES),
        (EconomicGoalContract, _ECONOMIC_GOAL_FIELD_DESCRIPTOR_WITNESSES),
        (
            ProposedTicketRiskContext,
            _PROPOSED_CONTEXT_FIELD_DESCRIPTOR_WITNESSES,
        ),
        (
            RiskOfRuinEvidence,
            _RISK_OF_RUIN_EVIDENCE_FIELD_DESCRIPTOR_WITNESSES,
        ),
        (TicketLeg, _TICKET_LEG_FIELD_DESCRIPTOR_WITNESSES),
        (MarketEvent, _MARKET_EVENT_FIELD_DESCRIPTOR_WITNESSES),
        (PaperTicket, _PAPER_TICKET_FIELD_DESCRIPTOR_WITNESSES),
    ):
        for name, expected_descriptor in witnesses:
            if owner.__dict__.get(name) is not expected_descriptor:
                return False

    try:
        _require_class_transition_graph(
            PaperRiskPolicy,
            methods=_RISK_POLICY_TRANSITION_METHOD_WITNESS,
            globals_witness=_RISK_POLICY_TRANSITION_GLOBAL_WITNESS,
            error="paper risk transition graph changed",
        )
    except RuntimeError:
        return False

    for (
        helper_name,
        expected_descriptor,
        expected_function,
        expected_code,
        descriptor_wrapped,
    ) in _ADMISSION_RISK_HELPER_WITNESSES:
        current_descriptor = PaperRiskPolicy.__dict__.get(helper_name)
        if current_descriptor is not expected_descriptor:
            return False
        current_function = (
            current_descriptor.__func__
            if descriptor_wrapped
            else current_descriptor
        )
        if (
            current_function is not expected_function
            or current_function.__code__ is not expected_code
        ):
            return False

    for owner, witnesses in (
        (
            ProposedTicketRiskContext,
            _PROPOSED_CONTEXT_EXECUTABLE_WITNESSES,
        ),
        (TicketLeg, _TICKET_LEG_EXECUTABLE_WITNESSES),
        (MarketEvent, _MARKET_EVENT_EXECUTABLE_WITNESSES),
        (EconomicGoalContract, _ECONOMIC_GOAL_EXECUTABLE_WITNESSES),
        (PaperTicket, _PAPER_TICKET_EXECUTABLE_WITNESSES),
    ):
        try:
            _require_executable_descriptor_witnesses(
                owner,
                witnesses,
                error="paper risk data executable authority changed",
            )
        except RuntimeError:
            return False

    for (
        _label,
        function,
        expected_code,
        bindings,
    ) in _DETACHED_RISK_HELPER_WITNESSES:
        if type(function) is not FunctionType or function.__code__ is not expected_code:
            return False
        for name, namespace, expected_value, expected_binding_code in bindings:
            if namespace.get(name) is not expected_value:
                return False
            if expected_binding_code is not None and (
                type(expected_value) is not FunctionType
                or expected_value.__code__ is not expected_binding_code
            ):
                return False
    return True


def _resume_after_product_day_turnover(
    *,
    risk_policy: PaperRiskPolicy,
    book: PaperBook,
    amount: Decimal,
    context: ProposedTicketRiskContext,
    pre_evaluation_state: tuple[Decimal, Decimal, Decimal, int] | None,
) -> RiskDecision:
    """Continue canonical risk evaluation after replacing only turnover room."""

    if not _admission_risk_helper_authority_valid():
        return RiskDecision(
            False,
            "virtual bankroll risk helper authority is invalid",
        )

    goal = risk_policy.economic_goal
    state = _RISK_BOOK_STATE(PaperRiskPolicy, book)
    if goal is None or state is None or state != pre_evaluation_state:
        return RiskDecision(False, "virtual bankroll changed during risk evaluation")
    initial_bankroll, balance, committed_stake, _ = state

    history_rooms = _RISK_HISTORY_FROZEN(
        PaperRiskPolicy,
        book,
        goal,
        context=context,
    )
    if history_rooms is None:
        return RiskDecision(False, "virtual bankroll risk history is invalid")
    session_room, day_room, drawdown_room, _ = history_rooms
    for room, reason in (
        (session_room, "economic goal conservative session loss limit exceeded"),
        (day_room, "economic goal conservative day loss limit exceeded"),
        (drawdown_room, "economic goal drawdown limit exceeded"),
    ):
        if amount > room:
            return RiskDecision(False, reason)

    quote_decision = _RISK_QUOTE_FROZEN(goal, context)
    if quote_decision is not None:
        return quote_decision

    if goal.max_risk_of_ruin < Decimal("1"):
        ruin_decision = _RISK_RUIN_FROZEN(
            PaperRiskPolicy,
            book,
            amount,
            goal,
            context,
        )
        if ruin_decision is not None:
            return ruin_decision

    derived = _RISK_DERIVED_FROZEN(
        risk_policy,
        initial_bankroll,
        balance,
        committed_stake,
        amount,
    )
    if derived is None:
        return RiskDecision(False, "virtual bankroll state is invalid")
    (
        ticket_limit,
        aggregate_committed,
        committed_limit,
        remaining_balance,
        reserve_limit,
    ) = derived
    if amount > ticket_limit:
        return RiskDecision(False, "ticket exceeds configured bankroll fraction")
    if aggregate_committed > committed_limit:
        return RiskDecision(False, "aggregate committed stake limit exceeded")
    if remaining_balance < reserve_limit:
        return RiskDecision(False, "minimum virtual cash reserve would be violated")
    if _RISK_BOOK_STATE(PaperRiskPolicy, book) != state:
        return RiskDecision(False, "virtual bankroll changed during risk evaluation")
    return RiskDecision(True, "allowed")


def admit_paper_ticket(
    *,
    workspace: str | Path,
    book: PaperBook,
    risk_policy: PaperRiskPolicy,
    stake: Decimal | str,
    legs: tuple[TicketLeg, ...],
    reason: str,
    placed_at: str,
    context: ProposedTicketRiskContext | None = None,
    provider_source_ids: tuple[str, ...] = (),
    provider_accounts: tuple[tuple[str, str], ...] = (),
    bankroll_id: str | None = None,
    currency: str | None = None,
) -> PaperAdmissionResult:
    """Atomically evaluate canonical PAPER risk and open the ticket if allowed.

    This function does not create a second risk, turnover, reservation, or ledger
    authority. It composes PaperRiskPolicy + PaperBook mutation inside the canonical
    WorkspaceEconomicLock. When a current product-issued UTC-day turnover snapshot
    is available, only the policy's conservative whole-history turnover room may be
    replaced; every other canonical risk gate remains authoritative. Incomplete or
    stale day evidence always falls back to the old conservative behavior.

    The lock is intentionally acquired *before* risk evaluation. A contender that
    cannot acquire the lock fails closed through WorkspaceEconomicLock rather than
    evaluating against stale economic state.
    """

    if type(book) is not PaperBook:
        raise TypeError("book must be an exact PaperBook")
    if type(risk_policy) is not PaperRiskPolicy:
        raise TypeError("risk_policy must be an exact PaperRiskPolicy")
    if context is not None and type(context) is not ProposedTicketRiskContext:
        raise TypeError("context must be an exact ProposedTicketRiskContext or None")
    if type(legs) is not tuple or not legs:
        raise ValueError("legs must be a non-empty canonical tuple")
    _require_risk_decision_authority()
    if not _admission_risk_helper_authority_valid():
        return PaperAdmissionResult(
            risk=RiskDecision(
                False,
                "virtual bankroll risk helper authority is invalid",
            ),
            ticket=None,
            book=book,
        )

    amount = _positive_decimal(stake)
    _validate_context_binding(
        context=context,
        legs=legs,
        placed_at=placed_at,
        provider_source_ids=provider_source_ids,
        provider_accounts=provider_accounts,
        bankroll_id=bankroll_id,
        currency=currency,
    )

    root = _canonical_workspace_root(workspace)
    book_path = _ADMISSION_PATH_TRUEDIV(root, "paper_book.json")
    _require_workspace_lock_dispatch()
    day_turnover_snapshot = _prepare_paper_day_turnover_snapshot(
        root=root,
        book_path=book_path,
        risk_policy=risk_policy,
    )

    _require_workspace_lock_dispatch()
    _require_admission_recovery_gate_authority()
    with WorkspaceEconomicLock(root) as workspace_lock:
        _require_admission_recovery_gate_authority()
        registry_path = _ADMISSION_PATH_TRUEDIV(root, "run_registry.json")
        registry_missing = False
        try:
            _ADMISSION_PATH_LSTAT(registry_path)
        except FileNotFoundError:
            registry_missing = True
        else:
            registry = object.__new__(RunRegistry)
            _RUN_REGISTRY_INIT_FUNCTION(registry, registry_path)
            if _RUN_REGISTRY_IN_PROGRESS_FUNCTION(registry):
                raise UnresolvedExperimentError(
                    "Workspace has an unresolved economic run; repair it before PAPER admission."
                )
        if _TRANSACTION_RECOVERY_FUNCTION(root):
            raise UnresolvedExperimentError(
                "Workspace has unresolved transaction history; repair it before PAPER admission."
            )
        if registry_missing:
            transaction_root = _ADMISSION_PATH_TRUEDIV(
                root,
                _RUN_TRANSACTION_ROOT_NAME,
            )
            try:
                first_transaction = next(_ADMISSION_PATH_ITERDIR(transaction_root))
            except FileNotFoundError:
                pass
            except StopIteration:
                pass
            else:
                del first_transaction
                raise UnresolvedExperimentError(
                    "Workspace run registry is missing while transaction history exists; "
                    "repair it before PAPER admission."
                )

        if not _ADMISSION_PATH_EXISTS(book_path):
            raise FileNotFoundError(
                "canonical paper_book.json must already exist; "
                "bootstrap/recovery belongs to the product lifecycle"
            )
        _require_paperbook_admission_authority()
        canonical_book = _PAPERBOOK_LOAD_FUNCTION(PaperBook, book_path)

        try:
            _REQUIRE_CURRENT_BINDING(book, book_path)
        except (TypeError, ValueError):
            working_book = canonical_book
        else:
            if not _same_semantic_book_state(canonical_book, book):
                raise ValueError(
                    "supplied current PaperBook does not match canonical durable state"
                )
            working_book = book

        _require_paperbook_admission_authority()
        pre_evaluation_state = _RISK_BOOK_STATE(PaperRiskPolicy, working_book)
        goal = risk_policy.economic_goal
        day_authority: _ProductDayAdmissionAuthority | None = None
        effective_placed_at = placed_at
        if goal is not None:
            day_authority = _revalidated_product_day_admission_authority(
                snapshot=day_turnover_snapshot,
                root=root,
                book=working_book,
                risk_policy=risk_policy,
                placed_at=placed_at,
                workspace_lock=workspace_lock,
            )
            if day_authority is not None:
                effective_placed_at = _canonical_day_authority_field(
                    day_authority,
                    _ProductDayAdmissionAuthority,
                    "admission_ts",
                )

        _require_paperbook_admission_authority()
        if not _admission_risk_helper_authority_valid():
            decision = RiskDecision(
                False,
                "virtual bankroll risk helper authority is invalid",
            )
        else:
            decision = _RISK_EVALUATE_FROZEN(
                risk_policy,
                working_book,
                amount,
                context=context,
            )

        decision_allowed, decision_reason = _canonical_risk_decision_state(
            decision
        )

        # Quote freshness is admission-time truth, not caller-time truth. Keep the
        # original context for provenance-bound risk-of-ruin/history semantics, but
        # independently re-run the canonical quote gate against the product-owned
        # action instant before any path can become positive.
        if (
            goal is not None
            and day_authority is not None
            and context is not None
            and (
                decision_allowed
                or decision_reason == "economic goal turnover limit exceeded"
            )
        ):
            if not _admission_risk_helper_authority_valid():
                decision = RiskDecision(
                    False,
                    "virtual bankroll risk helper authority is invalid",
                )
            else:
                try:
                    quote_context = _quote_context_with_product_admission_time(
                        context,
                        _canonical_day_authority_field(
                            day_authority,
                            _ProductDayAdmissionAuthority,
                            "admission_ts",
                        ),
                    )
                except (TypeError, ValueError):
                    decision = RiskDecision(
                        False,
                        "economic goal product-time quote evidence is invalid",
                    )
                else:
                    authoritative_quote_decision = _RISK_QUOTE_FROZEN(
                        goal,
                        quote_context,
                    )
                    if authoritative_quote_decision is not None:
                        decision = authoritative_quote_decision

        decision_allowed, decision_reason = _canonical_risk_decision_state(
            decision
        )
        turnover_override_candidate = (
            not decision_allowed
            and decision_reason == "economic goal turnover limit exceeded"
            and context is not None
        )
        turnover_room = (
            None
            if day_authority is None
            else _canonical_day_authority_field(
                day_authority,
                _ProductDayAdmissionAuthority,
                "turnover_room",
            )
        )

        if decision_allowed and goal is not None:
            if day_authority is None:
                decision = RiskDecision(
                    False,
                    "economic goal current product action time unavailable",
                )
            elif amount > turnover_room:
                decision = RiskDecision(
                    False,
                    "economic goal turnover limit exceeded",
                )
        elif turnover_override_candidate:
            if turnover_room is not None and amount <= turnover_room:
                assert context is not None
                decision = _resume_after_product_day_turnover(
                    risk_policy=risk_policy,
                    book=working_book,
                    amount=amount,
                    context=context,
                    pre_evaluation_state=pre_evaluation_state,
                )
        decision_allowed, _ = _canonical_risk_decision_state(decision)
        if not decision_allowed:
            return PaperAdmissionResult(
                risk=decision,
                ticket=None,
                book=working_book,
            )

        # Positive mutation may only occur while crash/recovery, PaperBook,
        # risk/proposal executable authority and the exact approved bankroll state all
        # still match. Keep the risk/state reread last so it is adjacent to the
        # irreversible ticket mutation after the other final guards finish.
        _require_admission_recovery_gate_authority()
        _require_paperbook_admission_authority()
        _require_risk_decision_authority()
        if not _admission_risk_helper_authority_valid():
            return PaperAdmissionResult(
                risk=RiskDecision(
                    False,
                    "virtual bankroll risk helper authority is invalid",
                ),
                ticket=None,
                book=working_book,
            )
        if (
            pre_evaluation_state is None
            or _RISK_BOOK_STATE(PaperRiskPolicy, working_book)
            != pre_evaluation_state
        ):
            return PaperAdmissionResult(
                risk=RiskDecision(
                    False,
                    "virtual bankroll changed before PAPER admission mutation",
                ),
                ticket=None,
                book=working_book,
            )

        # The caller's current PaperBook view is evidence, not the durable commit
        # target. Keep it untouched until the mutation has been durably published:
        # any exception after open_ticket (chronology binding, authority reread,
        # filesystem publication or readback) must not leave a caller-visible
        # debited/open state that never became canonical.
        _require_paperbook_admission_authority()
        mutation_book = _PAPERBOOK_LOAD_FUNCTION(PaperBook, book_path)
        if (
            not _same_semantic_book_state(working_book, mutation_book)
            or _RISK_BOOK_STATE(PaperRiskPolicy, mutation_book)
            != pre_evaluation_state
        ):
            return PaperAdmissionResult(
                risk=RiskDecision(
                    False,
                    "virtual bankroll changed before PAPER admission mutation",
                ),
                ticket=None,
                book=working_book,
            )

        day_admission_permit: object | None = None
        if day_authority is not None:
            # Refresh both day authority and the product-owned action instant at the
            # last reversible point.  The earlier instant was used for risk work that
            # can itself consume wall time; reusing it here would let a quote age past
            # its owner freshness ceiling before the irreversible PaperBook mutation.
            _require_product_day_turnover_dispatch()
            _require_day_authority_data_descriptors()
            window_store = _canonical_day_authority_field(
                day_authority,
                _ProductDayAdmissionAuthority,
                "window_store",
            )
            window_evidence = _canonical_day_authority_field(
                day_authority,
                _ProductDayAdmissionAuthority,
                "window_evidence",
            )
            prior_admission_ts = _canonical_day_authority_field(
                day_authority,
                _ProductDayAdmissionAuthority,
                "admission_ts",
            )
            fresh_admission_ts = _product_clock_admission_timestamp(
                window_store=window_store,
                current_window=window_evidence,
            )
            prior_admission_time = _parse_utc_timestamp(prior_admission_ts)
            fresh_admission_time = (
                None
                if fresh_admission_ts is None
                else _parse_utc_timestamp(fresh_admission_ts)
            )
            if (
                prior_admission_time is None
                or fresh_admission_time is None
                or fresh_admission_time < prior_admission_time
            ):
                return PaperAdmissionResult(
                    risk=RiskDecision(
                        False,
                        "economic goal product action time changed before PAPER mutation",
                    ),
                    ticket=None,
                    book=working_book,
                )

            # Quote freshness is a mutation-time gate.  Re-run it on the refreshed
            # product instant, not merely on the earlier product-time snapshot.
            if goal is not None and context is not None:
                if not _admission_risk_helper_authority_valid():
                    return PaperAdmissionResult(
                        risk=RiskDecision(
                            False,
                            "virtual bankroll risk helper authority is invalid",
                        ),
                        ticket=None,
                        book=working_book,
                    )
                try:
                    final_quote_context = _quote_context_with_product_admission_time(
                        context,
                        fresh_admission_ts,
                    )
                except (TypeError, ValueError):
                    return PaperAdmissionResult(
                        risk=RiskDecision(
                            False,
                            "economic goal product-time quote evidence is invalid",
                        ),
                        ticket=None,
                        book=working_book,
                    )
                final_quote_decision = _RISK_QUOTE_FROZEN(
                    goal,
                    final_quote_context,
                )
                if final_quote_decision is not None:
                    final_quote_allowed, _ = _canonical_risk_decision_state(
                        final_quote_decision
                    )
                    if not final_quote_allowed:
                        return PaperAdmissionResult(
                            risk=final_quote_decision,
                            ticket=None,
                            book=working_book,
                        )

            effective_placed_at = fresh_admission_ts
            try:
                day_admission_permit = (
                    _PAPERBOOK_PREPARE_PRODUCT_DAY_ADMISSION_FUNCTION(
                        mutation_book,
                        admission_ts=fresh_admission_ts,
                        window_store=window_store,
                        window_evidence=window_evidence,
                        workspace_lock=workspace_lock,
                    )
                )
            except RiskDayWindowMismatchError:
                return PaperAdmissionResult(
                    risk=RiskDecision(
                        False,
                        "economic goal product-day authority changed before PAPER mutation",
                    ),
                    ticket=None,
                    book=working_book,
                )
            _require_product_day_turnover_dispatch()
            _require_day_authority_data_descriptors()
            _require_paperbook_admission_authority()

        opened = _PAPERBOOK_OPEN_TICKET_FUNCTION(
            mutation_book,
            legs,
            amount,
            reason=reason,
            placed_at=effective_placed_at,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )
        if day_admission_permit is not None:
            _PAPERBOOK_RECORD_PRODUCT_DAY_ADMISSION_FUNCTION(
                mutation_book,
                opened.ticket_id,
                permit=day_admission_permit,
            )
        _require_paperbook_admission_authority()
        _PAPERBOOK_SAVE_FUNCTION(mutation_book, book_path)

        # Durable publication is the commit point. If the caller supplied the exact
        # current generation-bound view, preserve the long-standing in-place success
        # contract only *after* that commit by promoting the committed semantic state
        # and advancing the existing binding through the canonical persistence
        # authority. Before this point caller state has remained untouched.
        if working_book is book:
            book.initial_bankroll = mutation_book.initial_bankroll
            book.balance = mutation_book.balance
            book.tickets = dict(mutation_book.tickets)
            book._lifecycle = list(mutation_book._lifecycle)
            book._settlement_times = dict(mutation_book._settlement_times)
            book._product_day_admissions = dict(
                mutation_book._product_day_admissions
            )
            _paperbook_authority._INSTALL_OPENING(book)
            _paperbook_authority._INSTALL_CAUSAL(book)
            _paperbook_authority._advance_book_binding(book, book_path)

        _require_paperbook_admission_authority()
        persisted = _PAPERBOOK_LOAD_FUNCTION(PaperBook, book_path)
        persisted_ticket = persisted.tickets.get(opened.ticket_id)
        if persisted_ticket is None:
            raise RuntimeError(
                "persisted PaperBook lost the ticket opened inside admission"
            )
        if not _same_semantic_book_state(mutation_book, persisted):
            raise RuntimeError(
                "persisted PaperBook state does not match the admitted mutation"
            )
        result_book = book if working_book is book else persisted
        if not _same_semantic_book_state(mutation_book, result_book):
            raise RuntimeError(
                "current PaperBook view does not match the admitted durable mutation"
            )
        return PaperAdmissionResult(
            risk=decision,
            ticket=result_book.tickets[persisted_ticket.ticket_id],
            book=result_book,
        )



def _seal_admission_consumer_closure_authority(
    function: FunctionType,
) -> FunctionType:
    """Make the current-binding wrapper's executable closure immutable and witnessed.

    The current-binding consumer reconstructs the inner function from closure-owned
    globals on every call. For economic admission those globals already come from
    the frozen admission graph and must remain immutable authority.
    """

    if type(function) is not FunctionType:
        raise TypeError("canonical PAPER admission consumer must be a Python function")
    closure = function.__closure__
    if closure is None:
        raise RuntimeError("canonical PAPER admission consumer closure is unavailable")

    freevars = function.__code__.co_freevars
    if len(freevars) != len(closure):
        raise RuntimeError("canonical PAPER admission consumer closure is incomplete")

    # The current-binding sealer consumes these mappings through dict(...) and
    # get(...). A read-only proxy preserves those reads while closing in-place
    # check-to-dispatch mutation.
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError as exc:
            raise RuntimeError(
                "canonical PAPER admission consumer closure is incomplete"
            ) from exc
        if type(value) is dict:
            cell.cell_contents = MappingProxyType(dict(value))

    closure_witness: list[tuple[str, object, object]] = []
    mapping_function_witnesses: list[
        tuple[object, tuple[tuple[object, object, object | None], ...]]
    ] = []
    for name, cell in zip(freevars, closure, strict=True):
        try:
            value = cell.cell_contents
        except ValueError as exc:
            raise RuntimeError(
                "canonical PAPER admission consumer closure is incomplete"
            ) from exc
        closure_witness.append((name, cell, value))
        if type(value) is MappingProxyType:
            entries = tuple(
                (
                    key,
                    item,
                    item.__code__ if type(item) is FunctionType else None,
                )
                for key, item in value.items()
            )
            mapping_function_witnesses.append((value, entries))

    expected_closure = tuple(closure_witness)
    expected_mapping_functions = tuple(mapping_function_witnesses)
    expected_code = function.__code__
    expected_function = function
    exact_type = type
    function_type = FunctionType
    mapping_proxy_type = MappingProxyType

    def require_authority() -> None:
        if (
            exact_type(expected_function) is not function_type
            or expected_function.__code__ is not expected_code
            or expected_function.__closure__ is not closure
        ):
            raise RuntimeError("PAPER admission sealed consumer authority changed")
        current_closure = expected_function.__closure__
        if current_closure is None or len(current_closure) != len(expected_closure):
            raise RuntimeError("PAPER admission sealed consumer authority changed")
        for index, (_name, expected_cell, expected_value) in enumerate(
            expected_closure
        ):
            current_cell = current_closure[index]
            if current_cell is not expected_cell:
                raise RuntimeError("PAPER admission sealed consumer authority changed")
            try:
                current_value = current_cell.cell_contents
            except ValueError as exc:
                raise RuntimeError(
                    "PAPER admission sealed consumer authority changed"
                ) from exc
            if current_value is not expected_value:
                raise RuntimeError("PAPER admission sealed consumer authority changed")
        for mapping, entries in expected_mapping_functions:
            if exact_type(mapping) is not mapping_proxy_type:
                raise RuntimeError("PAPER admission sealed consumer authority changed")
            if tuple(mapping.keys()) != tuple(entry[0] for entry in entries):
                raise RuntimeError("PAPER admission sealed consumer authority changed")
            for key, expected_value, expected_value_code in entries:
                if mapping.get(key) is not expected_value:
                    raise RuntimeError(
                        "PAPER admission sealed consumer authority changed"
                    )
                if expected_value_code is not None and (
                    exact_type(expected_value) is not function_type
                    or expected_value.__code__ is not expected_value_code
                ):
                    raise RuntimeError(
                        "PAPER admission sealed consumer authority changed"
                    )

    require_authority_code = require_authority.__code__

    def guarded(*args: object, **kwargs: object) -> object:
        if (
            exact_type(require_authority) is not function_type
            or require_authority.__code__ is not require_authority_code
        ):
            raise RuntimeError("PAPER admission closure guard authority changed")
        require_authority()
        result = expected_function(*args, **kwargs)
        require_authority()
        return result

    guarded.__name__ = function.__name__
    guarded.__qualname__ = function.__qualname__
    guarded.__doc__ = function.__doc__
    guarded.__annotations__ = dict(function.__annotations__)
    return guarded


def _freeze_admission_module_globals() -> dict[str, object]:
    """Detach the complete admission helper graph from mutable module dispatch."""

    source = globals()
    frozen: dict[str, object] = dict(source)
    for name, value in tuple(source.items()):
        if type(value) is not FunctionType or value.__globals__ is not source:
            continue
        clone = FunctionType(
            value.__code__,
            frozen,
            name=value.__name__,
            argdefs=value.__defaults__,
            closure=value.__closure__,
        )
        if value.__kwdefaults__ is not None:
            clone.__kwdefaults__ = dict(value.__kwdefaults__)
        clone.__qualname__ = value.__qualname__
        clone.__doc__ = value.__doc__
        clone.__annotations__ = dict(value.__annotations__)
        frozen[name] = clone
    return frozen


from ._paperbook_current_binding_verifier import (
    seal_current_binding_consumer as _seal_current_binding_consumer,
)

_ADMISSION_FROZEN_GLOBALS = _freeze_admission_module_globals()
_FROZEN_ADMIT_PAPER_TICKET = _ADMISSION_FROZEN_GLOBALS["admit_paper_ticket"]
if type(_FROZEN_ADMIT_PAPER_TICKET) is not FunctionType:
    raise RuntimeError("canonical PAPER admission executable is unavailable")
admit_paper_ticket = _seal_admission_consumer_closure_authority(
    _seal_current_binding_consumer(_FROZEN_ADMIT_PAPER_TICKET)
)
del _FROZEN_ADMIT_PAPER_TICKET
del _ADMISSION_FROZEN_GLOBALS
del _seal_current_binding_consumer
