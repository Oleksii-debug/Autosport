from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from types import FunctionType, MethodType
from typing import Callable, Protocol

from .causal_collector import (
    CanonicalDesktopApplication,
    CausalView,
    CollectorDelta,
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    DesktopDeltaConsumer,
    canonical_event_digest,
)
from .collector_service import CollectorServiceSource, HeadlessCollectorService
from .continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionStatus,
    ContinuousTickResult,
    SessionState,
    SettlementLearningHandoff,
    SettlementOutcomeAuthority,
)
from .domain import MarketEvent
from .event_lifecycle import ContinuousEventLifecycle
from .ingestion_health import SourceHealthStore
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .market_bus import MarketEventBus
from .market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
    MarketMirror,
)
from .paper import PaperBook
from .resolver_semantics import ResolverSemanticIdentityError, function_semantic_sha256
from .storage import SQLiteMarketStore
from .workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
    WorkspaceEconomicLockError,
)


class ProductCompositionError(RuntimeError):
    """The durable product composition cannot be verified safely."""


def _build_product_desktop_consumer_type(
    base_type: type[DesktopDeltaConsumer],
    error_type: type[ProductCompositionError],
):
    """Build one product consumer whose authority snapshot is not instance-writable."""

    from weakref import WeakKeyDictionary

    snapshots = WeakKeyDictionary()
    protected_fields = frozenset(
        {
            "collector",
            "checkpoint",
            "resolve_event",
            "apply_event",
            "lookup_application_receipt",
            "_acknowledgement_clock",
            "_on_application_receipt",
            "_acknowledged_at",
            "drain",
            "__class__",
            "__dict__",
            "_PROTECTED_AUTHORITY_FIELDS",
            "_SNAPSHOT_FIELDS",
            "_product_authority_snapshot",
            "_product_authority_sealed",
        }
    )
    snapshot_fields = (
        "collector",
        "checkpoint",
        "resolve_event",
        "apply_event",
        "lookup_application_receipt",
        "_acknowledgement_clock",
        "_on_application_receipt",
    )
    base_init = base_type.__init__
    base_drain = base_type.drain
    missing = object()

    class ProductDesktopClassIdentity:
        __slots__ = ()

        def __get__(self, _instance, owner=None):
            return owner

        def __set__(self, _instance, _value) -> None:
            raise error_type("product desktop class identity is immutable")

        def __delete__(self, _instance) -> None:
            raise error_type("product desktop class identity is immutable")

    class ProductDesktopClassGuard:
        __slots__ = ("descriptor", "name")

        def __init__(self, name: str, descriptor: object) -> None:
            self.name = name
            self.descriptor = descriptor

        def __get__(self, _instance, _owner=None):
            return self.descriptor

        def __set__(self, _instance, _value) -> None:
            raise error_type(
                f"product desktop class member {self.name!r} is immutable"
            )

        def __delete__(self, _instance) -> None:
            raise error_type(
                f"product desktop class member {self.name!r} is immutable"
            )

    class ProductDesktopMeta(type(base_type)):
        pass

    def sealed_drain(
        self,
        *,
        as_of: str,
        view: CausalView = CausalView.AS_KNOWN_AT_DECISION,
    ) -> tuple[str, ...]:
        snapshot = snapshots.get(self)
        if snapshot is None:
            raise error_type("product desktop authority snapshot is unavailable")
        raw = object.__getattribute__(self, "__dict__")
        if "drain" in raw:
            raise error_type("product desktop drain authority changed after composition")
        for name, expected in snapshot:
            if raw.get(name, missing) is not expected:
                raise error_type(
                    f"product desktop authority field {name!r} changed after composition"
                )
        return base_drain(self, as_of=as_of, view=view)

    class ProductDesktopDeltaConsumer(
        base_type,
        metaclass=ProductDesktopMeta,
    ):
        """Freeze and continuously re-prove the product-owned desktop authority graph."""

        __class__ = ProductDesktopClassIdentity()
        _PROTECTED_AUTHORITY_FIELDS = protected_fields
        _SNAPSHOT_FIELDS = snapshot_fields
        _product_authority_snapshot = None
        _product_authority_sealed = True

        def __init__(self, *args, **kwargs) -> None:
            base_init(self, *args, **kwargs)
            snapshots[self] = tuple(
                (name, object.__getattribute__(self, name))
                for name in snapshot_fields
            )

        def __getattribute__(self, name: str):
            snapshot = snapshots.get(self)
            if snapshot is not None:
                if name == "drain":
                    return sealed_drain.__get__(self, type(self))
                if name in snapshot_fields:
                    for field_name, expected in snapshot:
                        if field_name == name:
                            return expected
            return object.__getattribute__(self, name)

        def __setattr__(self, name: str, value: object) -> None:
            if snapshots.get(self) is not None and name in protected_fields:
                raise error_type(
                    f"product desktop authority field {name!r} is immutable"
                )
            object.__setattr__(self, name, value)

    for name in ("__getattribute__", "__setattr__"):
        descriptor = ProductDesktopDeltaConsumer.__dict__[name]
        type.__setattr__(
            ProductDesktopMeta,
            name,
            ProductDesktopClassGuard(name, descriptor),
        )

    return ProductDesktopDeltaConsumer


_ProductDesktopDeltaConsumer = _build_product_desktop_consumer_type(
    DesktopDeltaConsumer,
    ProductCompositionError,
)
del _build_product_desktop_consumer_type


def _build_product_coordinator_type(
    base_type: type[ContinuousSessionCoordinator],
    error_type: type[ProductCompositionError],
):
    """Freeze the product coordinator's composed authority graph after construction."""

    from weakref import WeakKeyDictionary

    snapshots = WeakKeyDictionary()
    snapshot_fields = (
        "workspace",
        "collector",
        "lifecycle",
        "market_store",
        "desktop_consumer",
        "invalidation_buffer",
        "dependency_index",
        "paper_book_path",
        "outcome_authority",
        "settlement_learning_handoff",
        "clock",
        "required_history",
        "max_invalidation_batches_per_tick",
        "max_invalidation_items_per_batch",
        "causal_view",
        "initial_bankroll",
        "_state",
    )
    entry_methods = (
        "status",
        "pause",
        "resume",
        "stop",
        "tick",
        "_settlement_resolutions",
    )
    protected_fields = frozenset(
        {
            *snapshot_fields,
            *entry_methods,
            "__class__",
            "__dict__",
        }
    )
    base_methods = {
        name: getattr(base_type, name)
        for name in entry_methods
    }
    base_init = base_type.__init__
    missing = object()

    class ProductCoordinatorClassIdentity:
        __slots__ = ()

        def __get__(self, _instance, owner=None):
            return owner

        def __set__(self, _instance, _value) -> None:
            raise error_type("product coordinator class identity is immutable")

        def __delete__(self, _instance) -> None:
            raise error_type("product coordinator class identity is immutable")

    class ProductCoordinatorClassGuard:
        __slots__ = ("descriptor", "name")

        def __init__(self, name: str, descriptor: object) -> None:
            self.name = name
            self.descriptor = descriptor

        def __get__(self, _instance, _owner=None):
            return self.descriptor

        def __set__(self, _instance, _value) -> None:
            raise error_type(
                f"product coordinator class member {self.name!r} is immutable"
            )

        def __delete__(self, _instance) -> None:
            raise error_type(
                f"product coordinator class member {self.name!r} is immutable"
            )

    class ProductCoordinatorMeta(type(base_type)):
        pass

    def require_snapshot(self) -> tuple[tuple[str, object], ...]:
        snapshot = snapshots.get(self)
        if snapshot is None:
            raise error_type("product coordinator authority snapshot is unavailable")
        raw = object.__getattribute__(self, "__dict__")
        for name in entry_methods:
            if name in raw:
                raise error_type(
                    f"product coordinator method {name!r} changed after composition"
                )
        for name, expected in snapshot:
            if raw.get(name, missing) is not expected:
                raise error_type(
                    f"product coordinator authority field {name!r} changed after composition"
                )
        return snapshot

    class ProductContinuousSessionCoordinator(
        base_type,
        metaclass=ProductCoordinatorMeta,
    ):
        """Product-only coordinator with immutable composed authority references."""

        __class__ = ProductCoordinatorClassIdentity()

        def __init__(self, *args, **kwargs) -> None:
            base_init(self, *args, **kwargs)
            snapshots[self] = tuple(
                (name, object.__getattribute__(self, name))
                for name in snapshot_fields
            )

        def __getattribute__(self, name: str):
            snapshot = snapshots.get(self)
            if snapshot is not None:
                if name in dispatch_methods:
                    require_snapshot(self)
                    if name in static_dispatch_methods:
                        return base_methods[name]
                    return base_methods[name].__get__(self, type(self))
                if name in snapshot_fields:
                    for field_name, expected in snapshot:
                        if field_name == name:
                            return expected
            return object.__getattribute__(self, name)

        def __setattr__(self, name: str, value: object) -> None:
            if snapshots.get(self) is not None and name in protected_fields:
                raise error_type(
                    f"product coordinator authority field {name!r} is immutable"
                )
            object.__setattr__(self, name, value)

    for name in ("__getattribute__", "__setattr__"):
        descriptor = ProductContinuousSessionCoordinator.__dict__[name]
        type.__setattr__(
            ProductCoordinatorMeta,
            name,
            ProductCoordinatorClassGuard(name, descriptor),
        )

    return ProductContinuousSessionCoordinator


_ProductContinuousSessionCoordinator = _build_product_coordinator_type(
    ContinuousSessionCoordinator,
    ProductCompositionError,
)
del _build_product_coordinator_type


def _serialized_runtime_operation(method):
    """Hold one runtime-local fence across an admitted public lifecycle operation."""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._operation_fence:
            return method(self, *args, **kwargs)

    return guarded


class _ProductRuntimeLease(WorkspaceEconomicLock):
    """Crash-releasing single-process authority for one canonical product workspace."""

    FILE_NAME = ".product-runtime.lock"
    _PRODUCT_AUTHORITY_FIELDS = frozenset(
        {
            "_product_authority_fields_sealed",
            "_authority_active",
            "_acquired_once",
            "_operation_fence",
        }
    )

    def __setattr__(self, name: str, value: object) -> None:
        try:
            sealed = object.__getattribute__(
                self,
                "_product_authority_fields_sealed",
            )
        except AttributeError:
            sealed = False
        if sealed and name in (
            "_product_authority_fields_sealed",
            "_authority_active",
            "_acquired_once",
            "_operation_fence",
        ):
            raise WorkspaceEconomicLockError(
                f"product runtime lease authority field {name!r} is immutable"
            )
        object.__setattr__(self, name, value)

    def __init__(self, workspace: str | Path) -> None:
        object.__setattr__(self, "_product_authority_fields_sealed", False)
        super().__init__(workspace)
        object.__setattr__(self, "_authority_active", False)
        object.__setattr__(self, "_acquired_once", False)
        object.__setattr__(self, "_operation_fence", None)
        object.__setattr__(self, "_product_authority_fields_sealed", True)

    def bind_operation_fence(self, operation_fence: RLock) -> None:
        """Bind runtime release to the same in-process lifecycle serialization fence."""
        if object.__getattribute__(self, "_operation_fence") is not None:
            raise WorkspaceEconomicLockError(
                "product runtime operation fence is already bound"
            )
        object.__setattr__(self, "_operation_fence", operation_fence)

    @property
    def authority_active(self) -> bool:
        """Whether this one-shot lease still grants positive runtime authority."""
        return bool(object.__getattribute__(self, "_authority_active"))

    def acquire(self) -> None:
        if object.__getattribute__(self, "_acquired_once"):
            raise WorkspaceEconomicLockError(
                "product runtime workspace authority cannot be reacquired"
            )
        super().acquire()
        object.__setattr__(self, "_acquired_once", True)
        object.__setattr__(self, "_authority_active", True)

    def release(self) -> None:
        operation_fence = object.__getattribute__(self, "_operation_fence")
        if operation_fence is None:
            object.__setattr__(self, "_authority_active", False)
            super().release()
            return
        with operation_fence:
            object.__setattr__(self, "_authority_active", False)
            super().release()


def _build_product_runtime_lease_authority(
    *,
    base_init,
    base_acquire,
    base_release,
    error_type,
):
    def initialize(lease, workspace: str | Path) -> None:
        object.__setattr__(lease, "_product_authority_fields_sealed", False)
        base_init(lease, workspace)
        object.__setattr__(lease, "_authority_active", False)
        object.__setattr__(lease, "_acquired_once", False)
        object.__setattr__(lease, "_operation_fence", None)
        object.__setattr__(lease, "_product_authority_fields_sealed", True)

    def acquire(lease) -> None:
        if object.__getattribute__(lease, "_acquired_once"):
            raise error_type(
                "product runtime workspace authority cannot be reacquired"
            )
        base_acquire(lease)
        object.__setattr__(lease, "_acquired_once", True)
        object.__setattr__(lease, "_authority_active", True)

    def release(lease) -> None:
        operation_fence = object.__getattribute__(lease, "_operation_fence")
        if operation_fence is None:
            object.__setattr__(lease, "_authority_active", False)
            base_release(lease)
            return
        with operation_fence:
            object.__setattr__(lease, "_authority_active", False)
            base_release(lease)

    def bind_operation_fence(lease, operation_fence: RLock) -> None:
        if object.__getattribute__(lease, "_operation_fence") is not None:
            raise error_type(
                "product runtime operation fence is already bound"
            )
        object.__setattr__(lease, "_operation_fence", operation_fence)

    def authority_active(lease) -> bool:
        return bool(object.__getattribute__(lease, "_authority_active"))

    return (
        initialize,
        acquire,
        release,
        bind_operation_fence,
        authority_active,
    )


(
    _product_runtime_lease_init,
    _product_runtime_lease_acquire,
    _product_runtime_lease_release,
    _product_runtime_lease_bind_operation_fence,
    _product_runtime_lease_authority_active,
) = _build_product_runtime_lease_authority(
    base_init=WorkspaceEconomicLock.__init__,
    base_acquire=WorkspaceEconomicLock.acquire,
    base_release=WorkspaceEconomicLock.release,
    error_type=WorkspaceEconomicLockError,
)
del _build_product_runtime_lease_authority


class _ProductStartTransitionStore:
    """Durable START transaction journal under the runtime-wide workspace lease."""

    _SCHEMA = "autosport.product_runtime_start_transition"
    _VERSION = 1
    _PHASES = frozenset(
        {"STARTING", "COMPLETED", "ROLLED_BACK", "RECOVERY_REQUIRED"}
    )
    _FIELDS = frozenset(
        {
            "schema",
            "schema_version",
            "generation",
            "phase",
            "collector_was_stopped",
            "session_pre_state",
        }
    )
    _PENDING_PHASES = frozenset({"STARTING", "RECOVERY_REQUIRED"})

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, object] | None:
        schema = "autosport.product_runtime_start_transition"
        version = 1
        phases = frozenset(
            {"STARTING", "COMPLETED", "ROLLED_BACK", "RECOVERY_REQUIRED"}
        )
        fields = frozenset(
            {
                "schema",
                "schema_version",
                "generation",
                "phase",
                "collector_was_stopped",
                "session_pre_state",
            }
        )
        if not self.path.exists():
            return None
        path = object.__getattribute__(self, "path")
        loads = object.__getattribute__(self, "_loads")
        text_validator = object.__getattribute__(self, "_text_validator")
        try:
            raw = loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ProductCompositionError(
                "cannot verify durable product START transition"
            ) from exc
        if (
            type(raw) is not dict
            or set(raw) != fields
            or raw.get("schema") != schema
            or raw.get("schema_version") != version
        ):
            raise ProductCompositionError(
                "durable product START transition schema mismatch"
            )
        generation = raw.get("generation")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            raise ProductCompositionError(
                "durable product START transition generation is invalid"
            )
        phase = raw.get("phase")
        if phase not in phases:
            raise ProductCompositionError(
                "durable product START transition phase is invalid"
            )
        collector_was_stopped = raw.get("collector_was_stopped")
        if type(collector_was_stopped) is not bool:
            raise ProductCompositionError(
                "durable product START transition collector pre-state is invalid"
            )
        session_pre_state = raw.get("session_pre_state")
        if session_pre_state not in {
            SessionState.RUNNING.value,
            SessionState.PAUSED.value,
            SessionState.STOPPED.value,
        }:
            raise ProductCompositionError(
                "durable product START transition session pre-state is invalid"
            )
        if collector_was_stopped != (
            session_pre_state == SessionState.STOPPED.value
        ):
            raise ProductCompositionError(
                "durable product START transition pre-state is incoherent"
            )
        return raw

    def pending(self) -> dict[str, object] | None:
        raw = self._read()
        if raw is None or raw["phase"] not in frozenset(
            {"STARTING", "RECOVERY_REQUIRED"}
        ):
            return None
        return dict(raw)

    def begin(
        self,
        *,
        collector_was_stopped: bool,
        session_pre_state: str,
    ) -> int:
        if type(collector_was_stopped) is not bool:
            raise ProductCompositionError(
                "product START collector pre-state must be boolean"
            )
        if session_pre_state not in {
            SessionState.RUNNING.value,
            SessionState.PAUSED.value,
            SessionState.STOPPED.value,
        }:
            raise ProductCompositionError(
                "product START session pre-state is invalid"
            )
        if collector_was_stopped != (
            session_pre_state == SessionState.STOPPED.value
        ):
            raise ProductCompositionError(
                "product START pre-state authorities disagree"
            )
        current = self._read()
        if current is not None and current["phase"] in frozenset(
            {"STARTING", "RECOVERY_REQUIRED"}
        ):
            raise ProductCompositionError(
                "unfinished product START transition requires recovery"
            )
        generation = 1 if current is None else int(current["generation"]) + 1
        atomic_write_json(
            self.path,
            {
                "schema": "autosport.product_runtime_start_transition",
                "schema_version": 1,
                "generation": generation,
                "phase": "STARTING",
                "collector_was_stopped": collector_was_stopped,
                "session_pre_state": session_pre_state,
            },
        )
        verified = self._read()
        if (
            verified is None
            or verified["generation"] != generation
            or verified["phase"] != "STARTING"
        ):
            raise ProductCompositionError(
                "durable product START transition publication could not be verified"
            )
        return generation

    def _mark(
        self,
        generation: int,
        phase: str,
        *,
        allowed_from: frozenset[str],
    ) -> None:
        current = self._read()
        if current is None or current["generation"] != generation:
            raise ProductCompositionError(
                "durable product START transition generation changed"
            )
        current_phase = current["phase"]
        if current_phase == phase:
            return
        if current_phase not in allowed_from:
            raise ProductCompositionError(
                "durable product START transition phase changed unexpectedly"
            )
        updated = dict(current)
        updated["phase"] = phase
        atomic_write_json(self.path, updated)
        verified = self._read()
        if (
            verified is None
            or verified["generation"] != generation
            or verified["phase"] != phase
        ):
            raise ProductCompositionError(
                "durable product START transition update could not be verified"
            )

    def mark_completed(self, generation: int) -> None:
        self._mark(
            generation,
            "COMPLETED",
            allowed_from=frozenset({"STARTING"}),
        )

    def mark_rolled_back(self, generation: int) -> None:
        self._mark(
            generation,
            "ROLLED_BACK",
            allowed_from=frozenset({"STARTING", "RECOVERY_REQUIRED"}),
        )

    def mark_recovery_required(self, generation: int) -> None:
        self._mark(
            generation,
            "RECOVERY_REQUIRED",
            allowed_from=frozenset({"STARTING", "RECOVERY_REQUIRED"}),
        )


def _read_product_start_transition_impl(
    store,
    *,
    _loads,
    _error_type,
    _object_getattribute,
    _running_state: str,
    _paused_state: str,
    _stopped_state: str,
) -> dict[str, object] | None:
    path = _object_getattribute(store, "path")
    if not path.exists():
        return None
    try:
        raw = _loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise _error_type(
            "cannot verify durable product START transition"
        ) from exc
    fields = frozenset(
        {
            "schema",
            "schema_version",
            "generation",
            "phase",
            "collector_was_stopped",
            "session_pre_state",
        }
    )
    if (
        type(raw) is not dict
        or set(raw) != fields
        or raw.get("schema") != "autosport.product_runtime_start_transition"
        or raw.get("schema_version") != 1
    ):
        raise _error_type(
            "durable product START transition schema mismatch"
        )
    generation = raw.get("generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
    ):
        raise _error_type(
            "durable product START transition generation is invalid"
        )
    phase = raw.get("phase")
    if phase not in frozenset(
        {"STARTING", "COMPLETED", "ROLLED_BACK", "RECOVERY_REQUIRED"}
    ):
        raise _error_type(
            "durable product START transition phase is invalid"
        )
    collector_was_stopped = raw.get("collector_was_stopped")
    if type(collector_was_stopped) is not bool:
        raise _error_type(
            "durable product START transition collector pre-state is invalid"
        )
    session_pre_state = raw.get("session_pre_state")
    if session_pre_state not in {
        _running_state,
        _paused_state,
        _stopped_state,
    }:
        raise _error_type(
            "durable product START transition session pre-state is invalid"
        )
    if collector_was_stopped != (session_pre_state == _stopped_state):
        raise _error_type(
            "durable product START transition pre-state is incoherent"
        )
    return raw


def _bind_product_start_transition_reader(implementation):
    loads = strict_json_loads
    error_type = ProductCompositionError
    object_getattribute = object.__getattribute__
    running_state = SessionState.RUNNING.value
    paused_state = SessionState.PAUSED.value
    stopped_state = SessionState.STOPPED.value

    def bound(store) -> dict[str, object] | None:
        return implementation(
            store,
            _loads=loads,
            _error_type=error_type,
            _object_getattribute=object_getattribute,
            _running_state=running_state,
            _paused_state=paused_state,
            _stopped_state=stopped_state,
        )

    return bound


_read_product_start_transition = _bind_product_start_transition_reader(
    _read_product_start_transition_impl
)
del _read_product_start_transition_impl
del _bind_product_start_transition_reader


class ProductCollectorSource(CollectorServiceSource, Protocol):
    """One acquisition source plus canonical delta-to-event resolution.

    Provider credentials and network policy remain outside this composition root. The
    root owns only how the already-normalized source is connected to Autosport's
    canonical durable authorities.
    """

    def resolve_event(self, delta: CollectorDelta) -> MarketEvent:
        ...


@dataclass(frozen=True, slots=True)
class ProductCompositionManifest:
    source_id: str
    initial_bankroll: str
    settlement_authority_identity: str | None = None
    source_resolver_identity: str | None = None
    settlement_learning_handoff_identity: str | None = None


class _ManifestStore:
    _SCHEMA = "autosport.autonomous_product_composition"
    _VERSION = 4
    _V1_FIELDS = frozenset({"schema", "schema_version", "source_id", "initial_bankroll"})
    _V2_FIELDS = frozenset(
        {
            "schema",
            "schema_version",
            "source_id",
            "initial_bankroll",
            "settlement_authority_identity",
        }
    )
    _V3_FIELDS = frozenset(
        {
            "schema",
            "schema_version",
            "source_id",
            "initial_bankroll",
            "source_resolver_identity",
            "settlement_authority_identity",
        }
    )
    _FIELDS = frozenset(
        {
            "schema",
            "schema_version",
            "source_id",
            "initial_bankroll",
            "source_resolver_identity",
            "settlement_authority_identity",
            "settlement_learning_handoff_identity",
        }
    )

    def __init__(
        self,
        path: Path,
        *,
        _loads=None,
        _write=None,
        _text_validator=None,
        _read_raw_authority=None,
        _manifest_type=None,
    ) -> None:
        self.path = path
        self._loads = strict_json_loads if _loads is None else _loads
        self._write = atomic_write_json if _write is None else _write
        self._text_validator = (
            _ManifestStore._text
            if _text_validator is None
            else _text_validator
        )
        self._read_raw_authority = (
            _ManifestStore._read_raw
            if _read_raw_authority is None
            else _read_raw_authority
        )
        self._manifest_type = (
            ProductCompositionManifest
            if _manifest_type is None
            else _manifest_type
        )

    @staticmethod
    def _text(value: object, field: str) -> str:
        if type(value) is not str or not value or value.strip() != value:
            raise ProductCompositionError(f"{field} must be a non-empty trimmed string")
        return value

    def _read_raw(self) -> dict[str, object]:
        schema = "autosport.autonomous_product_composition"
        current_version = 4
        v1_fields = frozenset(
            {"schema", "schema_version", "source_id", "initial_bankroll"}
        )
        v2_fields = frozenset(
            {
                "schema",
                "schema_version",
                "source_id",
                "initial_bankroll",
                "settlement_authority_identity",
            }
        )
        v3_fields = frozenset(
            {
                "schema",
                "schema_version",
                "source_id",
                "initial_bankroll",
                "source_resolver_identity",
                "settlement_authority_identity",
            }
        )
        current_fields = frozenset(
            {
                "schema",
                "schema_version",
                "source_id",
                "initial_bankroll",
                "source_resolver_identity",
                "settlement_authority_identity",
                "settlement_learning_handoff_identity",
            }
        )
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ProductCompositionError("cannot verify product composition manifest") from exc
        if type(raw) is not dict or raw.get("schema") != schema:
            raise ProductCompositionError("product composition manifest schema mismatch")
        version = raw.get("schema_version")
        if version == 1 and set(raw) == v1_fields:
            text_validator(raw.get("source_id"), "source_id")
            text_validator(raw.get("initial_bankroll"), "initial_bankroll")
            return {
                **raw,
                "source_resolver_identity": None,
                "settlement_authority_identity": None,
                "settlement_learning_handoff_identity": None,
            }
        if version == 2 and set(raw) == v2_fields:
            text_validator(raw.get("source_id"), "source_id")
            text_validator(raw.get("initial_bankroll"), "initial_bankroll")
            return {
                **raw,
                "source_resolver_identity": None,
                "settlement_learning_handoff_identity": None,
            }
        if version == 3 and set(raw) == v3_fields:
            text_validator(raw.get("source_id"), "source_id")
            text_validator(raw.get("initial_bankroll"), "initial_bankroll")
            return {
                **raw,
                "settlement_learning_handoff_identity": None,
            }
        if version != current_version or set(raw) != current_fields:
            raise ProductCompositionError("product composition manifest schema mismatch")
        text_validator(raw.get("source_id"), "source_id")
        text_validator(raw.get("initial_bankroll"), "initial_bankroll")
        source_identity = raw.get("source_resolver_identity")
        if source_identity is not None:
            source_identity = text_validator(
                source_identity,
                "source_resolver_identity",
            )
            if (
                len(source_identity) != 64
                or source_identity != source_identity.lower()
                or any(
                    character not in "0123456789abcdef"
                    for character in source_identity
                )
            ):
                raise ProductCompositionError(
                    "source_resolver_identity must be lowercase SHA-256 hex"
                )
        authority_identity = raw.get("settlement_authority_identity")
        if authority_identity is not None:
            identity = text_validator(
                authority_identity,
                "settlement_authority_identity",
            )
            if (
                len(identity) != 64
                or identity != identity.lower()
                or any(character not in "0123456789abcdef" for character in identity)
            ):
                raise ProductCompositionError(
                    "settlement_authority_identity must be lowercase SHA-256 hex"
                )
        learning_identity = raw.get("settlement_learning_handoff_identity")
        if learning_identity is not None:
            identity = text_validator(
                learning_identity,
                "settlement_learning_handoff_identity",
            )
            if (
                len(identity) != 64
                or identity != identity.lower()
                or any(character not in "0123456789abcdef" for character in identity)
            ):
                raise ProductCompositionError(
                    "settlement_learning_handoff_identity must be lowercase SHA-256 hex"
                )
        return raw

    def load_or_create(
        self,
        *,
        source_id: str,
        initial_bankroll: str,
        source_resolver_identity: str,
        settlement_authority_identity: str | None,
        settlement_learning_handoff_identity: str | None,
    ) -> ProductCompositionManifest:
        path = object.__getattribute__(self, "path")
        write = object.__getattribute__(self, "_write")
        text_validator = object.__getattribute__(self, "_text_validator")
        read_raw = object.__getattribute__(self, "_read_raw_authority")
        manifest_type = object.__getattribute__(self, "_manifest_type")
        source_id = text_validator(source_id, "source_id")
        initial_bankroll = text_validator(initial_bankroll, "initial_bankroll")
        source_resolver_identity = text_validator(
            source_resolver_identity,
            "source_resolver_identity",
        )
        if (
            len(source_resolver_identity) != 64
            or source_resolver_identity != source_resolver_identity.lower()
            or any(
                character not in "0123456789abcdef"
                for character in source_resolver_identity
            )
        ):
            raise ProductCompositionError(
                "source_resolver_identity must be lowercase SHA-256 hex"
            )
        if settlement_authority_identity is not None:
            settlement_authority_identity = text_validator(
                settlement_authority_identity,
                "settlement_authority_identity",
            )
        if settlement_learning_handoff_identity is not None:
            settlement_learning_handoff_identity = text_validator(
                settlement_learning_handoff_identity,
                "settlement_learning_handoff_identity",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            write(
                path,
                {
                    "schema": "autosport.autonomous_product_composition",
                    "schema_version": 4,
                    "source_id": source_id,
                    "initial_bankroll": initial_bankroll,
                    "source_resolver_identity": source_resolver_identity,
                    "settlement_authority_identity": settlement_authority_identity,
                    "settlement_learning_handoff_identity": settlement_learning_handoff_identity,
                },
            )
        raw = read_raw(self)
        if raw["source_id"] != source_id:
            raise ProductCompositionError(
                "configured source_id conflicts with durable product composition"
            )
        if raw["initial_bankroll"] != initial_bankroll:
            raise ProductCompositionError(
                "configured initial_bankroll conflicts with durable product composition"
            )
        if raw["source_resolver_identity"] != source_resolver_identity:
            raise ProductCompositionError(
                "source resolver identity conflicts with durable product composition"
            )
        if raw["settlement_authority_identity"] != settlement_authority_identity:
            raise ProductCompositionError(
                "settlement authority identity conflicts with durable product composition"
            )
        if (
            raw["settlement_learning_handoff_identity"]
            != settlement_learning_handoff_identity
        ):
            raise ProductCompositionError(
                "settlement learning handoff identity conflicts with durable product composition"
            )
        return manifest_type(
            source_id=source_id,
            initial_bankroll=initial_bankroll,
            source_resolver_identity=source_resolver_identity,
            settlement_authority_identity=settlement_authority_identity,
            settlement_learning_handoff_identity=settlement_learning_handoff_identity,
        )


def _source_resolver_identity_impl(
    *,
    source: ProductCollectorSource,
    source_id: str,
    _text,
    _semantic_sha256,
    _semantic_error_type,
    _error_type,
    _dumps,
    _sha256,
    _function_type,
    _method_type,
    _object_getattribute,
) -> str:
    """Fingerprint the durable delta-to-MarketEvent authority used across restart."""

    source_id = _text(source_id, "source_id")
    if getattr(source, "source_id", None) != source_id:
        raise _error_type(
            "source resolver identity conflicts with configured source_id"
        )
    source_type = type(source)
    if source_type.__getattribute__ is not _object_getattribute:
        raise _error_type(
            "product source must use canonical object attribute lookup"
        )
    if any("__getattr__" in vars(owner) for owner in source_type.__mro__):
        raise _error_type(
            "product source cannot define fallback attribute dispatch"
        )
    instance_dict = getattr(source, "__dict__", None)
    source_methods: dict[str, tuple[FunctionType, str]] = {}
    for method_name in ("resolve_event", "fetch_catalog_page", "fetch_deltas"):
        if type(instance_dict) is dict and method_name in instance_dict:
            raise _error_type(
                f"product source forbids per-instance {method_name} shadowing"
            )
        method = getattr(source_type, method_name, None)
        if type(method) is not _function_type:
            raise _error_type(
                f"product source must use a concrete class {method_name} method"
            )
        bound_method = getattr(source, method_name, None)
        if (
            type(bound_method) is not _method_type
            or bound_method.__self__ is not source
            or bound_method.__func__ is not method
        ):
            raise _error_type(
                f"product source {method_name} instance dispatch is not canonical"
            )
        if method.__defaults__ is not None or method.__kwdefaults__ not in (None, {}):
            raise _error_type(
                f"product source {method_name} cannot use mutable call defaults"
            )
        if method.__closure__ is not None:
            raise _error_type(
                f"product source {method_name} cannot close over mutable authority"
            )
        try:
            semantic_sha256 = _semantic_sha256(
                method,
                runtime_owner=source_type,
            )
        except _semantic_error_type as exc:
            raise _error_type(
                f"product source {method_name} semantics cannot be fingerprinted safely"
            ) from exc
        source_methods[method_name] = (method, semantic_sha256)

    resolver, resolver_semantic_sha256 = source_methods["resolve_event"]
    catalog_fetch, catalog_fetch_semantic_sha256 = source_methods[
        "fetch_catalog_page"
    ]
    delta_fetch, delta_fetch_semantic_sha256 = source_methods["fetch_deltas"]

    def optional_text(name: str) -> str | None:
        value = None
        if type(instance_dict) is dict and name in instance_dict:
            value = instance_dict[name]
        else:
            for owner in type(source).__mro__:
                if name in vars(owner):
                    raw = vars(owner)[name]
                    if type(raw) is not str:
                        raise _error_type(
                            f"{name} must be a concrete string authority value"
                        )
                    value = raw
                    break
        if value is None:
            return None
        return _text(value, name)

    explicit_configuration = optional_text(
        "product_source_configuration_sha256"
    )
    authority_binding = optional_text("_authority_binding_sha256")
    for field_name, digest in (
        ("product_source_configuration_sha256", explicit_configuration),
        ("_authority_binding_sha256", authority_binding),
    ):
        if digest is not None and (
            len(digest) != 64
            or digest != digest.lower()
            or any(
                character not in "0123456789abcdef"
                for character in digest
            )
        ):
            raise _error_type(
                f"{field_name} must be lowercase SHA-256 hex"
            )

    declared_authority_fields = getattr(
        source_type,
        "_AUTHORITY_FIELDS",
        frozenset(),
    )
    if type(declared_authority_fields) is not frozenset or any(
        type(name) is not str or not name
        for name in declared_authority_fields
    ):
        raise _error_type(
            "product source _AUTHORITY_FIELDS must be a frozenset of non-empty strings"
        )

    payload = {
        "source_id": source_id,
        "implementation": f"{type(source).__module__}.{type(source).__qualname__}",
        "authority_fields": sorted(declared_authority_fields),
        "resolver_owner": f"{resolver.__module__}.{resolver.__qualname__}",
        "resolver_semantic_sha256": resolver_semantic_sha256,
        "catalog_fetch_owner": (
            f"{catalog_fetch.__module__}.{catalog_fetch.__qualname__}"
        ),
        "catalog_fetch_semantic_sha256": catalog_fetch_semantic_sha256,
        "delta_fetch_owner": f"{delta_fetch.__module__}.{delta_fetch.__qualname__}",
        "delta_fetch_semantic_sha256": delta_fetch_semantic_sha256,
        "configuration_sha256": explicit_configuration,
        "authority_binding_sha256": authority_binding,
        "lawful_terms_ref": optional_text("lawful_terms_ref"),
        "retention_ref": optional_text("retention_ref"),
    }
    encoded = _dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(encoded).hexdigest()


def _bind_source_resolver_identity(implementation):
    text = _ManifestStore._text
    semantic_sha256 = function_semantic_sha256
    semantic_error_type = ResolverSemanticIdentityError
    error_type = ProductCompositionError
    dumps = json.dumps
    sha256 = hashlib.sha256
    function_type = FunctionType
    method_type = MethodType
    object_getattribute = object.__getattribute__

    def bound(
        *,
        source: ProductCollectorSource,
        source_id: str,
    ) -> str:
        return implementation(
            source=source,
            source_id=source_id,
            _text=text,
            _semantic_sha256=semantic_sha256,
            _semantic_error_type=semantic_error_type,
            _error_type=error_type,
            _dumps=dumps,
            _sha256=sha256,
            _function_type=function_type,
            _method_type=method_type,
            _object_getattribute=object_getattribute,
        )

    return bound


_source_resolver_identity = _bind_source_resolver_identity(
    _source_resolver_identity_impl
)
del _source_resolver_identity_impl
del _bind_source_resolver_identity

def _settlement_authority_identity_impl(
    *,
    source: ProductCollectorSource,
    source_id: str,
    outcome_authority: SettlementOutcomeAuthority | None,
    _text,
    _semantic_sha256,
    _semantic_error_type,
    _error_type,
    _function_type,
    _dumps,
    _sha256,
) -> str | None:
    if outcome_authority is None:
        return None
    if outcome_authority is not source:
        raise _error_type(
            "settlement outcome authority must be owned by the configured product source"
        )
    source_id = _text(source_id, "source_id")
    instance_dict = getattr(source, "__dict__", None)
    if type(instance_dict) is dict and "resolve" in instance_dict:
        raise _error_type(
            "source-owned settlement authority forbids per-instance resolve shadowing"
        )
    resolver = getattr(type(source), "resolve", None)
    if type(resolver) is not _function_type:
        raise _error_type(
            "source-owned settlement authority must use a concrete class resolve method"
        )
    if resolver.__defaults__ is not None or resolver.__kwdefaults__ not in (None, {}):
        raise _error_type(
            "source-owned settlement resolve method cannot use mutable call defaults"
        )
    if resolver.__closure__ is not None:
        raise _error_type(
            "source-owned settlement resolve method cannot close over mutable authority"
        )
    try:
        resolver_semantic_sha256 = _semantic_sha256(
            resolver,
            runtime_owner=type(source),
        )
    except _semantic_error_type as exc:
        raise _error_type(
            "source-owned settlement resolve semantics cannot be fingerprinted safely"
        ) from exc
    resolver_owner = _text(
        f"{resolver.__module__}.{resolver.__qualname__}",
        "settlement resolver owner",
    )
    declared_implementation_id = getattr(
        type(source),
        "settlement_resolver_implementation_id",
        None,
    )
    if declared_implementation_id is None:
        raise _error_type(
            "source-owned settlement authority must declare stable settlement_resolver_implementation_id"
        )
    if (
        type(instance_dict) is dict
        and "settlement_resolver_implementation_id" in instance_dict
    ):
        raise _error_type(
            "source-owned settlement authority forbids per-instance implementation identity shadowing"
        )
    resolver_implementation_id = _text(
        declared_implementation_id,
        "settlement_resolver_implementation_id",
    )
    authority_id = _text(
        getattr(source, "settlement_authority_id", None),
        "settlement_authority_id",
    )
    configuration_sha256 = _text(
        getattr(source, "settlement_configuration_sha256", None),
        "settlement_configuration_sha256",
    )
    if (
        len(configuration_sha256) != 64
        or configuration_sha256 != configuration_sha256.lower()
        or any(
            character not in "0123456789abcdef"
            for character in configuration_sha256
        )
    ):
        raise _error_type(
            "settlement_configuration_sha256 must be lowercase SHA-256 hex"
        )
    implementation = f"{type(source).__module__}.{type(source).__qualname__}"
    payload = {
        "source_id": source_id,
        "authority_id": authority_id,
        "configuration_sha256": configuration_sha256,
        "implementation": implementation,
        "resolver_owner": resolver_owner,
        "resolver_implementation_id": resolver_implementation_id,
        "resolver_semantic_sha256": resolver_semantic_sha256,
    }
    encoded = _dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(encoded).hexdigest()


def _bind_settlement_authority_identity(implementation):
    text = _ManifestStore._text
    semantic_sha256 = function_semantic_sha256
    semantic_error_type = ResolverSemanticIdentityError
    error_type = ProductCompositionError
    function_type = FunctionType
    dumps = json.dumps
    sha256 = hashlib.sha256

    def bound(
        *,
        source: ProductCollectorSource,
        source_id: str,
        outcome_authority: SettlementOutcomeAuthority | None,
    ) -> str | None:
        return implementation(
            source=source,
            source_id=source_id,
            outcome_authority=outcome_authority,
            _text=text,
            _semantic_sha256=semantic_sha256,
            _semantic_error_type=semantic_error_type,
            _error_type=error_type,
            _function_type=function_type,
            _dumps=dumps,
            _sha256=sha256,
        )

    return bound


_settlement_authority_identity = _bind_settlement_authority_identity(
    _settlement_authority_identity_impl
)
del _settlement_authority_identity_impl
del _bind_settlement_authority_identity


def _settlement_learning_handoff_identity_impl(
    *,
    handoff: SettlementLearningHandoff | None,
    _text,
    _semantic_sha256,
    _semantic_error_type,
    _error_type,
    _function_type,
    _method_type,
    _object_getattribute,
    _dumps,
    _sha256,
) -> str | None:
    if handoff is None:
        return None
    handoff_type = type(handoff)
    if handoff_type.__getattribute__ is not _object_getattribute:
        raise _error_type(
            "settlement learning handoff must use canonical object attribute lookup"
        )
    if any("__getattr__" in vars(owner) for owner in handoff_type.__mro__):
        raise _error_type(
            "settlement learning handoff cannot define fallback attribute dispatch"
        )
    instance_dict = getattr(handoff, "__dict__", None)
    methods: dict[str, tuple[str, str] | None] = {}
    for method_name, required in (
        ("prepare_settlement", False),
        ("prepared_settlement_resolutions", False),
        ("reconcile_after_settlement", True),
    ):
        if type(instance_dict) is dict and method_name in instance_dict:
            raise _error_type(
                f"settlement learning handoff forbids per-instance {method_name} shadowing"
            )
        method = getattr(handoff_type, method_name, None)
        if method is None and not required:
            methods[method_name] = None
            continue
        if type(method) is not _function_type:
            raise _error_type(
                f"settlement learning handoff must use a concrete class {method_name} method"
            )
        bound_method = getattr(handoff, method_name, None)
        if (
            type(bound_method) is not _method_type
            or bound_method.__self__ is not handoff
            or bound_method.__func__ is not method
        ):
            raise _error_type(
                f"settlement learning handoff {method_name} dispatch is not canonical"
            )
        if method.__defaults__ is not None or method.__kwdefaults__ not in (None, {}):
            raise _error_type(
                f"settlement learning handoff {method_name} cannot use mutable call defaults"
            )
        if method.__closure__ is not None:
            raise _error_type(
                f"settlement learning handoff {method_name} cannot close over mutable authority"
            )
        try:
            semantic_sha256 = _semantic_sha256(
                method,
                runtime_owner=handoff_type,
            )
        except _semantic_error_type as exc:
            raise _error_type(
                f"settlement learning handoff {method_name} semantics cannot be fingerprinted safely"
            ) from exc
        methods[method_name] = (
            _text(
                f"{method.__module__}.{method.__qualname__}",
                f"settlement learning {method_name} owner",
            ),
            semantic_sha256,
        )

    declared_implementation_id = getattr(
        handoff_type,
        "settlement_learning_handoff_implementation_id",
        None,
    )
    if declared_implementation_id is None:
        raise _error_type(
            "settlement learning handoff must declare stable settlement_learning_handoff_implementation_id"
        )
    if (
        type(instance_dict) is dict
        and "settlement_learning_handoff_implementation_id" in instance_dict
    ):
        raise _error_type(
            "settlement learning handoff forbids per-instance implementation identity shadowing"
        )
    implementation_id = _text(
        declared_implementation_id,
        "settlement_learning_handoff_implementation_id",
    )
    configuration_sha256 = _text(
        getattr(handoff, "settlement_learning_configuration_sha256", None),
        "settlement_learning_configuration_sha256",
    )
    if (
        len(configuration_sha256) != 64
        or configuration_sha256 != configuration_sha256.lower()
        or any(
            character not in "0123456789abcdef"
            for character in configuration_sha256
        )
    ):
        raise _error_type(
            "settlement_learning_configuration_sha256 must be lowercase SHA-256 hex"
        )

    declared_authority_fields = getattr(
        handoff_type,
        "_AUTHORITY_FIELDS",
        frozenset(),
    )
    if type(declared_authority_fields) is not frozenset or any(
        type(name) is not str or not name
        for name in declared_authority_fields
    ):
        raise _error_type(
            "settlement learning _AUTHORITY_FIELDS must be a frozenset of non-empty strings"
        )

    prepare = methods["prepare_settlement"]
    prepared_recovery = methods["prepared_settlement_resolutions"]
    reconcile = methods["reconcile_after_settlement"]
    payload = {
        "implementation": f"{handoff_type.__module__}.{handoff_type.__qualname__}",
        "authority_fields": sorted(declared_authority_fields),
        "implementation_id": implementation_id,
        "configuration_sha256": configuration_sha256,
        "prepare_owner": None if prepare is None else prepare[0],
        "prepare_semantic_sha256": None if prepare is None else prepare[1],
        "prepared_recovery_owner": (
            None if prepared_recovery is None else prepared_recovery[0]
        ),
        "prepared_recovery_semantic_sha256": (
            None if prepared_recovery is None else prepared_recovery[1]
        ),
        "reconcile_owner": reconcile[0] if reconcile is not None else None,
        "reconcile_semantic_sha256": reconcile[1] if reconcile is not None else None,
    }
    encoded = _dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(encoded).hexdigest()


def _bind_settlement_learning_handoff_identity(implementation):
    text = _ManifestStore._text
    semantic_sha256 = function_semantic_sha256
    semantic_error_type = ResolverSemanticIdentityError
    error_type = ProductCompositionError
    function_type = FunctionType
    method_type = MethodType
    object_getattribute = object.__getattribute__
    dumps = json.dumps
    sha256 = hashlib.sha256

    def bound(
        *,
        handoff: SettlementLearningHandoff | None,
    ) -> str | None:
        return implementation(
            handoff=handoff,
            _text=text,
            _semantic_sha256=semantic_sha256,
            _semantic_error_type=semantic_error_type,
            _error_type=error_type,
            _function_type=function_type,
            _method_type=method_type,
            _object_getattribute=object_getattribute,
            _dumps=dumps,
            _sha256=sha256,
        )

    return bound


_settlement_learning_handoff_identity = _bind_settlement_learning_handoff_identity(
    _settlement_learning_handoff_identity_impl
)
del _settlement_learning_handoff_identity_impl
del _bind_settlement_learning_handoff_identity


def _desktop_applied_current_for_source_impl(
    *,
    source_id: str,
    market_store: SQLiteMarketStore,
    canonical_application: CanonicalDesktopApplication,
    _event_type,
    _receipt_type,
    _completed_receipts,
    _validate_receipt,
    _market_events,
    _canonical_digest,
    _dedupe_getter,
    _quote_getter,
) -> tuple[MarketEvent, ...]:
    """Rebuild restart state only from completed canonical desktop applications.

    Generic/import market history remains valid audit evidence but is insufficient
    decision authority. Completed desktop-application receipts are retained
    independently of collector-delta compaction and bind the exact canonical event
    payload through its digest.
    """

    try:
        receipts = _completed_receipts(canonical_application, source_id)
    except Exception as exc:
        raise ProductCompositionError(
            "cannot verify desktop application receipts for product runtime restart"
        ) from exc

    receipt_digests: set[str] = set()
    for receipt in receipts:
        if type(receipt) is not _receipt_type:
            raise ProductCompositionError(
                "desktop application receipt type is not canonical"
            )
        try:
            _validate_receipt(receipt)
        except Exception as exc:
            raise ProductCompositionError(
                "desktop application receipt is invalid"
            ) from exc
        receipt_digests.add(receipt.canonical_event_digest)

    if not receipt_digests:
        return ()

    try:
        history = _market_events(market_store)
    except Exception as exc:
        raise ProductCompositionError(
            "cannot verify canonical market history for product runtime restart"
        ) from exc

    matched_digests: set[str] = set()
    latest: dict[tuple[str, str], MarketEvent] = {}
    for event in history:
        if type(event) is not _event_type:
            raise ProductCompositionError(
                "market history returned a non-canonical event type"
            )
        if event.source_id != source_id:
            continue
        try:
            if _dedupe_getter is None or _quote_getter is None:
                raise RuntimeError(
                    "canonical MarketEvent identity descriptor is unavailable"
                )
            event_dedupe_key = _dedupe_getter(event)
            digest = _canonical_digest(event)
            quote_key = _quote_getter(event)
        except Exception as exc:
            raise ProductCompositionError(
                "cannot verify canonical market identity for product runtime restart"
            ) from exc
        if digest not in receipt_digests:
            continue
        matched_digests.add(digest)
        key = (event.source_id, quote_key)
        previous = latest.get(key)
        previous_dedupe_key = (
            None if previous is None else _dedupe_getter(previous)
        )
        if previous is None or (event.sequence, event_dedupe_key) > (
            previous.sequence,
            previous_dedupe_key,
        ):
            latest[key] = event

    if matched_digests != receipt_digests:
        raise ProductCompositionError(
            "desktop application receipt references missing canonical market history"
        )
    return tuple(latest[key] for key in sorted(latest))


def _bind_desktop_applied_current_for_source(implementation):
    """Closure-bind receipt-backed restart authority dependencies."""

    event_type = MarketEvent
    receipt_type = DesktopApplicationReceipt
    completed_receipts = CanonicalDesktopApplication.verified_completed_receipts_for_source
    validate_receipt = DesktopApplicationReceipt.validate
    market_events = SQLiteMarketStore.events
    canonical_digest = canonical_event_digest
    dedupe_getter = MarketEvent.dedupe_key.fget
    quote_getter = MarketEvent.quote_key.fget

    def bound(
        *,
        source_id: str,
        market_store: SQLiteMarketStore,
        canonical_application: CanonicalDesktopApplication,
    ) -> tuple[MarketEvent, ...]:
        return implementation(
            source_id=source_id,
            market_store=market_store,
            canonical_application=canonical_application,
            _event_type=event_type,
            _receipt_type=receipt_type,
            _completed_receipts=completed_receipts,
            _validate_receipt=validate_receipt,
            _market_events=market_events,
            _canonical_digest=canonical_digest,
            _dedupe_getter=dedupe_getter,
            _quote_getter=quote_getter,
        )

    return bound


_desktop_applied_current_for_source = _bind_desktop_applied_current_for_source(
    _desktop_applied_current_for_source_impl
)
del _desktop_applied_current_for_source_impl
del _bind_desktop_applied_current_for_source


def _desktop_applied_event_for_receipt_impl(
    *,
    source_id: str,
    delta: CollectorDelta,
    receipt: DesktopApplicationReceipt,
    market_store: SQLiteMarketStore,
    _delta_type,
    _event_type,
    _receipt_type,
    _validate_receipt,
    _market_events,
    _canonical_digest,
    _dedupe_getter,
) -> MarketEvent:
    """Resolve one completed desktop receipt to its exact canonical market event."""
    if type(delta) is not _delta_type:
        raise ProductCompositionError("desktop delivery delta type is not canonical")
    if type(receipt) is not _receipt_type:
        raise ProductCompositionError("desktop delivery receipt type is not canonical")
    try:
        _validate_receipt(receipt)
    except Exception as exc:
        raise ProductCompositionError(
            "desktop delivery receipt is invalid"
        ) from exc
    if (
        delta.source_id != source_id
        or receipt.delta_id != delta.delta_id
        or receipt.canonical_event_digest != delta.canonical_event_digest
    ):
        raise ProductCompositionError(
            "desktop delivery receipt is not bound to this runtime delta"
        )
    if _dedupe_getter is None:
        raise ProductCompositionError(
            "canonical MarketEvent dedupe identity descriptor is unavailable"
        )
    try:
        history = _market_events(market_store, delta.event_id)
    except Exception as exc:
        raise ProductCompositionError(
            "cannot read canonical market history for desktop delivery"
        ) from exc

    matches: list[MarketEvent] = []
    for event in history:
        if type(event) is not _event_type:
            raise ProductCompositionError(
                "market history returned a non-canonical event type"
            )
        if event.source_id != source_id:
            continue
        try:
            event_dedupe_key = _dedupe_getter(event)
            event_digest = _canonical_digest(event)
        except Exception as exc:
            raise ProductCompositionError(
                "cannot verify canonical market identity for desktop delivery"
            ) from exc
        if (
            event_dedupe_key == delta.event_dedupe_key
            and event_digest == receipt.canonical_event_digest
        ):
            matches.append(event)

    if len(matches) != 1:
        raise ProductCompositionError(
            "desktop delivery receipt does not resolve to exactly one canonical market event"
        )
    return matches[0]


def _bind_desktop_applied_event_for_receipt(implementation):
    """Closure-bind post-receipt market resolution authority dependencies."""

    delta_type = CollectorDelta
    event_type = MarketEvent
    receipt_type = DesktopApplicationReceipt
    validate_receipt = DesktopApplicationReceipt.validate
    market_events = SQLiteMarketStore.events
    canonical_digest = canonical_event_digest
    dedupe_getter = MarketEvent.dedupe_key.fget

    def bound(
        *,
        source_id: str,
        delta: CollectorDelta,
        receipt: DesktopApplicationReceipt,
        market_store: SQLiteMarketStore,
    ) -> MarketEvent:
        return implementation(
            source_id=source_id,
            delta=delta,
            receipt=receipt,
            market_store=market_store,
            _delta_type=delta_type,
            _event_type=event_type,
            _receipt_type=receipt_type,
            _validate_receipt=validate_receipt,
            _market_events=market_events,
            _canonical_digest=canonical_digest,
            _dedupe_getter=dedupe_getter,
        )

    return bound


_desktop_applied_event_for_receipt = _bind_desktop_applied_event_for_receipt(
    _desktop_applied_event_for_receipt_impl
)
del _desktop_applied_event_for_receipt_impl
del _bind_desktop_applied_event_for_receipt


@dataclass(slots=True)
class AutonomousProductRuntime:
    """One supported headless composition of the integrated PAPER product authorities."""

    workspace: Path
    manifest: ProductCompositionManifest
    coordinator: ContinuousSessionCoordinator
    collector: HeadlessCollectorService
    market_store: SQLiteMarketStore
    lifecycle: ContinuousEventLifecycle
    mirror: MarketMirror
    invalidations: BoundedMirrorInvalidationBuffer
    dependencies: FocusedMirrorDependencyIndex
    _runtime_lease: _ProductRuntimeLease
    _start_transition_store: _ProductStartTransitionStore
    _closed: bool = False
    _operation_fence: RLock = field(
        default_factory=RLock,
        init=False,
        repr=False,
        compare=False,
    )

    def _require_runtime_authority(self) -> None:
        if self._closed or not self._runtime_lease.authority_active:
            raise ProductCompositionError(
                "product runtime is closed or no longer owns workspace authority"
            )

    @staticmethod
    def _state_value(status: ContinuousSessionStatus) -> str:
        state = getattr(status, "state", None)
        value = state.value if hasattr(state, "value") else state
        if value not in {
            SessionState.RUNNING.value,
            SessionState.PAUSED.value,
            SessionState.STOPPED.value,
        }:
            raise ProductCompositionError(
                "canonical product session returned an invalid lifecycle state"
            )
        return value

    def _require_start_transition_resolved(self) -> None:
        if self._start_transition_store.pending() is not None:
            raise ProductCompositionError(
                "product START transition requires recovery before positive lifecycle use"
            )

    def _coherent_status(
        self,
        *,
        allow_pending_start: bool = False,
    ) -> ContinuousSessionStatus:
        """Project lifecycle truth only when collector and session durable state agree."""

        self._require_runtime_authority()
        if not allow_pending_start:
            self._require_start_transition_resolved()
        coordinator_status = self.coordinator.status()
        state = self._state_value(coordinator_status)
        try:
            collector_status = self.collector.status()
        except Exception as exc:
            raise ProductCompositionError(
                "cannot verify canonical collector lifecycle state"
            ) from exc
        if (
            type(collector_status) is not dict
            or "stopped_at" not in collector_status
            or "stop_reason" not in collector_status
        ):
            raise ProductCompositionError(
                "canonical collector lifecycle state is incomplete"
            )
        stopped_at = collector_status["stopped_at"]
        stop_reason = collector_status["stop_reason"]
        if (stopped_at is None) != (stop_reason is None):
            raise ProductCompositionError(
                "canonical collector STOP state is incomplete"
            )
        collector_stopped = stopped_at is not None
        session_stopped = state == SessionState.STOPPED.value
        if collector_stopped != session_stopped:
            raise ProductCompositionError(
                "canonical product runtime lifecycle authorities disagree"
            )
        return coordinator_status

    @staticmethod
    def _note_secondary_failure(
        primary_error: BaseException,
        *,
        action: str,
        secondary_error: BaseException,
    ) -> None:
        try:
            primary_error.add_note(
                f"{action} also failed: "
                f"{type(secondary_error).__name__}: {secondary_error}"
            )
        except BaseException:
            pass

    def _mark_start_recovery_required(
        self,
        generation: int,
        primary_error: BaseException,
    ) -> None:
        try:
            self._start_transition_store.mark_recovery_required(generation)
        except BaseException as transition_error:
            self._note_secondary_failure(
                primary_error,
                action="START recovery journal",
                secondary_error=transition_error,
            )

    def _compensate_failed_start(
        self,
        primary_error: BaseException,
        *,
        generation: int,
    ) -> None:
        compensation_failed = False
        for action, stop in (
            ("collector STOP compensation", self.collector.stop),
            ("session STOP compensation", self.coordinator.stop),
        ):
            try:
                stop("runtime_start_failed")
            except BaseException as secondary_error:
                compensation_failed = True
                self._note_secondary_failure(
                    primary_error,
                    action=action,
                    secondary_error=secondary_error,
                )

        if not compensation_failed:
            try:
                status = self._coherent_status(allow_pending_start=True)
                if self._state_value(status) != SessionState.STOPPED.value:
                    raise ProductCompositionError(
                        "START compensation did not reach coherent STOPPED state"
                    )
            except BaseException as secondary_error:
                compensation_failed = True
                self._note_secondary_failure(
                    primary_error,
                    action="START compensation verification",
                    secondary_error=secondary_error,
                )

        if compensation_failed:
            self._mark_start_recovery_required(generation, primary_error)
            return

        try:
            self._start_transition_store.mark_rolled_back(generation)
        except BaseException as transition_error:
            self._note_secondary_failure(
                primary_error,
                action="START rollback journal",
                secondary_error=transition_error,
            )

    def _recover_interrupted_start(self) -> None:
        pending = self._start_transition_store.pending()
        if pending is None:
            return
        try:
            self.stop("runtime_start_recovery")
        except BaseException as exc:
            raise ProductCompositionError(
                "cannot recover interrupted product START transition"
            ) from exc

    @_serialized_runtime_operation
    def start(self) -> ContinuousSessionStatus:
        current = self._coherent_status()
        current_state = self._state_value(current)
        if current_state == SessionState.RUNNING.value:
            return current

        generation = self._start_transition_store.begin(
            collector_was_stopped=current_state == SessionState.STOPPED.value,
            session_pre_state=current_state,
        )
        try:
            self.collector.resume()
            self.coordinator.resume()
            resolved = self._coherent_status(allow_pending_start=True)
            if self._state_value(resolved) != SessionState.RUNNING.value:
                raise ProductCompositionError(
                    "product START did not reach coherent RUNNING state"
                )
            self._start_transition_store.mark_completed(generation)
            return resolved
        except BaseException as primary_error:
            self._compensate_failed_start(
                primary_error,
                generation=generation,
            )
            raise

    @_serialized_runtime_operation
    def pause(self) -> ContinuousSessionStatus:
        current = self._coherent_status()
        state = self._state_value(current)
        if state == SessionState.STOPPED.value:
            raise ProductCompositionError(
                "cannot pause a stopped product runtime; start it before pausing"
            )
        if state == SessionState.PAUSED.value:
            return current
        self.coordinator.pause()
        return self._coherent_status()

    @_serialized_runtime_operation
    def resume(self) -> ContinuousSessionStatus:
        return self.start()

    @_serialized_runtime_operation
    def stop(self, reason: str = "operator_stop") -> ContinuousSessionStatus:
        self._require_runtime_authority()
        pending = self._start_transition_store.pending()
        collector_error: BaseException | None = None
        coordinator_error: BaseException | None = None
        try:
            self.collector.stop(reason)
        except BaseException as exc:
            collector_error = exc
        try:
            self.coordinator.stop(reason)
        except BaseException as exc:
            coordinator_error = exc

        if collector_error is not None:
            if coordinator_error is not None:
                self._note_secondary_failure(
                    collector_error,
                    action="session STOP",
                    secondary_error=coordinator_error,
                )
            if pending is not None:
                self._mark_start_recovery_required(
                    int(pending["generation"]),
                    collector_error,
                )
            raise collector_error
        if coordinator_error is not None:
            if pending is not None:
                self._mark_start_recovery_required(
                    int(pending["generation"]),
                    coordinator_error,
                )
            raise coordinator_error

        resolved = self._coherent_status(allow_pending_start=True)
        if self._state_value(resolved) != SessionState.STOPPED.value:
            error = ProductCompositionError(
                "canonical product STOP did not reach coherent STOPPED state"
            )
            if pending is not None:
                self._mark_start_recovery_required(
                    int(pending["generation"]),
                    error,
                )
            raise error
        if pending is not None:
            self._start_transition_store.mark_rolled_back(
                int(pending["generation"])
            )
        return resolved

    @_serialized_runtime_operation
    def status(self) -> ContinuousSessionStatus:
        return self._coherent_status()

    @_serialized_runtime_operation
    def tick(self) -> ContinuousTickResult:
        self._coherent_status()
        return self.coordinator.tick()

    @_serialized_runtime_operation
    def close(self) -> None:
        object.__setattr__(self, "_closed", True)
        try:
            self.market_store.close()
        except BaseException as primary_error:
            try:
                self._runtime_lease.release()
            except BaseException as release_error:
                try:
                    primary_error.add_note(
                        "product runtime lease release also failed while closing "
                        f"market storage: {type(release_error).__name__}: {release_error}"
                    )
                except BaseException:
                    pass
            raise
        self._runtime_lease.release()


def _build_product_runtime_type(
    base_type: type[AutonomousProductRuntime],
    error_type: type[ProductCompositionError],
    market_store_close,
):
    """Freeze the top-level product runtime graph outside caller-writable slots."""

    from weakref import WeakKeyDictionary

    snapshots = WeakKeyDictionary()
    snapshot_fields = (
        "workspace",
        "manifest",
        "coordinator",
        "collector",
        "market_store",
        "lifecycle",
        "mirror",
        "invalidations",
        "dependencies",
        "_runtime_lease",
        "_start_transition_store",
        "_operation_fence",
    )
    entry_methods = ("start", "pause", "resume", "stop", "status", "tick", "close")
    internal_methods = (
        "_require_runtime_authority",
        "_state_value",
        "_require_start_transition_resolved",
        "_coherent_status",
        "_note_secondary_failure",
        "_mark_start_recovery_required",
        "_compensate_failed_start",
        "_recover_interrupted_start",
    )
    dispatch_methods = frozenset((*entry_methods, *internal_methods))
    static_dispatch_methods = frozenset(
        {"_state_value", "_note_secondary_failure"}
    )
    base_methods = {name: getattr(base_type, name) for name in dispatch_methods}
    base_init = base_type.__init__

    def product_close(self) -> None:
        object.__setattr__(self, "_closed", True)
        try:
            market_store_close(self.market_store)
        except BaseException as primary_error:
            try:
                self._runtime_lease.release()
            except BaseException as release_error:
                try:
                    primary_error.add_note(
                        "product runtime lease release also failed while closing "
                        f"market storage: {type(release_error).__name__}: {release_error}"
                    )
                except BaseException:
                    pass
            raise
        self._runtime_lease.release()

    base_methods["close"] = product_close

    class ProductRuntimeClassIdentity:
        __slots__ = ()

        def __get__(self, _instance, owner=None):
            return owner

        def __set__(self, _instance, _value) -> None:
            raise error_type("product runtime class identity is immutable")

        def __delete__(self, _instance) -> None:
            raise error_type("product runtime class identity is immutable")

    class ProductRuntimeClassGuard:
        __slots__ = ("descriptor", "name")

        def __init__(self, name: str, descriptor: object) -> None:
            self.name = name
            self.descriptor = descriptor

        def __get__(self, _instance, _owner=None):
            return self.descriptor

        def __set__(self, _instance, _value) -> None:
            raise error_type(
                f"product runtime class member {self.name!r} is immutable"
            )

        def __delete__(self, _instance) -> None:
            raise error_type(
                f"product runtime class member {self.name!r} is immutable"
            )

    class ProductRuntimeMeta(type(base_type)):
        pass

    def require_snapshot(self) -> tuple[tuple[str, object], ...]:
        snapshot = snapshots.get(self)
        if snapshot is None:
            raise error_type("product runtime authority snapshot is unavailable")
        raw = object.__getattribute__(self, "__dict__")
        for name in dispatch_methods:
            if name in raw:
                raise error_type(
                    f"product runtime method {name!r} changed after composition"
                )
        for name, expected in snapshot:
            if raw.get(name) is not expected:
                raise error_type(
                    f"product runtime authority field {name!r} changed after composition"
                )
        return snapshot

    class ProductAutonomousProductRuntime(
        base_type,
        metaclass=ProductRuntimeMeta,
    ):
        __class__ = ProductRuntimeClassIdentity()
        __eq__ = object.__eq__
        __hash__ = object.__hash__

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            snapshots[self] = tuple(
                (name, object.__getattribute__(self, name))
                for name in snapshot_fields
            )

        def __getattribute__(self, name: str):
            snapshot = snapshots.get(self)
            if snapshot is not None:
                if name in entry_methods:
                    require_snapshot(self)
                    return base_methods[name].__get__(self, type(self))
                if name in snapshot_fields:
                    for field_name, expected in snapshot:
                        if field_name == name:
                            return expected
            return object.__getattribute__(self, name)

        def __setattr__(self, name: str, value: object) -> None:
            snapshot = snapshots.get(self)
            if snapshot is not None and (
                name in snapshot_fields
                or name in dispatch_methods
                or name in {"__class__", "_closed"}
            ):
                raise error_type(
                    f"product runtime authority field {name!r} is immutable"
                )
            object.__setattr__(self, name, value)

    for name in ("__getattribute__", "__setattr__", "__eq__", "__hash__"):
        descriptor = ProductAutonomousProductRuntime.__dict__[name]
        type.__setattr__(
            ProductRuntimeMeta,
            name,
            ProductRuntimeClassGuard(name, descriptor),
        )

    return ProductAutonomousProductRuntime


_ProductAutonomousProductRuntime = _build_product_runtime_type(
    AutonomousProductRuntime,
    ProductCompositionError,
    SQLiteMarketStore.close,
)
del _build_product_runtime_type


def _build_autonomous_product_runtime_impl(
    *,
    workspace: str | Path,
    source: ProductCollectorSource,
    clock: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    initial_bankroll: str = "10000",
    outcome_authority: SettlementOutcomeAuthority | None = None,
    settlement_learning_handoff: SettlementLearningHandoff | None = None,
    _desktop_restart_reader,
    _desktop_delivery_resolver,
    _desktop_consumer_type,
    _desktop_consumer_init,
    _canonical_application_type,
    _canonical_application_init,
    _canonical_application_apply,
    _canonical_application_lookup,
    _paper_book_type,
    _paper_book_init,
    _runtime_type,
    _runtime_init,
    _runtime_lease_type,
    _runtime_lease_init,
    _runtime_lease_acquire,
    _runtime_lease_release,
    _runtime_lease_bind_operation_fence,
    _runtime_lease_authority_active_getter,
    _source_resolver_identity_fn,
    _settlement_authority_identity_fn,
    _settlement_learning_handoff_identity_fn,
    _manifest_store_type,
    _manifest_store_init,
    _manifest_store_load_or_create,
    _manifest_store_read_raw,
    _manifest_loads,
    _manifest_write,
    _manifest_text_validator,
    _manifest_type,
    _lifecycle_type,
    _lifecycle_init,
    _market_store_type,
    _market_store_init,
    _market_store_close,
    _mirror_type,
    _mirror_init,
    _invalidation_buffer_type,
    _invalidation_buffer_init,
    _market_bus_type,
    _market_bus_init,
    _source_health_type,
    _source_health_init,
    _dependency_index_type,
    _dependency_index_init,
    _collector_store_type,
    _collector_store_init,
    _collector_service_type,
    _collector_service_init,
    _collector_service_status,
    _collector_service_resume,
    _collector_service_stop,
    _collector_service_run_cycle,
    _collector_service_bounded_provider_call,
    _checkpoint_type,
    _checkpoint_init,
    _coordinator_type,
    _coordinator_init,
    _start_transition_store_type,
    _start_transition_store_init,
    _start_transition_read,
    _start_transition_write,
) -> AutonomousProductRuntime:
    """Construct or restore one canonical headless PAPER product runtime.

    This function deliberately composes existing Autosport authorities instead of
    recreating collector, market, lifecycle, settlement, or learning truth. The
    provider-specific source supplies only acquisition and canonical event resolution.
    Product-owned paths and identities are deterministic from ``workspace`` so restart
    reopens the same durable session rather than constructing a second runtime graph.
    """

    root = Path(workspace)
    source_id = getattr(source, "source_id", None)
    if type(source_id) is not str or not source_id or source_id.strip() != source_id:
        raise ProductCompositionError("source.source_id must be a non-empty trimmed string")
    if not callable(getattr(source, "resolve_event", None)):
        raise ProductCompositionError("source.resolve_event must be callable")

    try:
        normalized_bankroll = str(initial_bankroll)
        validation_book = object.__new__(_paper_book_type)
        _paper_book_init(validation_book, normalized_bankroll)
    except Exception as exc:
        raise ValueError("initial_bankroll must construct a valid PaperBook") from exc

    root.mkdir(parents=True, exist_ok=True)
    resolved_clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    lease_stack = ExitStack()
    raw_runtime_lease = object.__new__(_runtime_lease_type)
    try:
        _runtime_lease_init(raw_runtime_lease, root)
        _runtime_lease_acquire(raw_runtime_lease)
    except WorkspaceEconomicLockBusyError as exc:
        raise ProductCompositionError(
            "another Autosport product runtime already owns this workspace"
        ) from exc
    except WorkspaceEconomicLockError as exc:
        raise ProductCompositionError(
            "cannot establish exclusive product runtime workspace authority"
        ) from exc
    lease_stack.callback(_runtime_lease_release, raw_runtime_lease)

    with lease_stack:
        source_resolver_identity = _source_resolver_identity_fn(
            source=source,
            source_id=source_id,
        )
        settlement_authority_identity = _settlement_authority_identity_fn(
            source=source,
            source_id=source_id,
            outcome_authority=outcome_authority,
        )
        settlement_learning_handoff_identity = (
            _settlement_learning_handoff_identity_fn(
                handoff=settlement_learning_handoff,
            )
        )
        manifest_store = object.__new__(_manifest_store_type)
        _manifest_store_init(
            manifest_store,
            root / "product_composition.json",
            _loads=_manifest_loads,
            _write=_manifest_write,
            _text_validator=_manifest_text_validator,
            _read_raw_authority=_manifest_store_read_raw,
            _manifest_type=_manifest_type,
        )
        manifest = _manifest_store_load_or_create(
            manifest_store,
            source_id=source_id,
            initial_bankroll=normalized_bankroll,
            source_resolver_identity=source_resolver_identity,
            settlement_authority_identity=settlement_authority_identity,
            settlement_learning_handoff_identity=settlement_learning_handoff_identity,
        )

        declared_source_authority_fields = getattr(
            type(source),
            "_AUTHORITY_FIELDS",
            frozenset(),
        )
        if type(declared_source_authority_fields) is not frozenset or any(
            type(name) is not str or not name
            for name in declared_source_authority_fields
        ):
            raise ProductCompositionError(
                "product source _AUTHORITY_FIELDS must be a frozenset of non-empty strings"
            )
        source_authority_snapshot: tuple[tuple[str, object], ...] = tuple(
            (
                name,
                object.__getattribute__(source, name),
            )
            for name in sorted(
                declared_source_authority_fields.difference({"stream_epoch"})
            )
        )

        def require_declared_source_authority_roots() -> None:
            for name, expected in source_authority_snapshot:
                try:
                    current = object.__getattribute__(source, name)
                except AttributeError as exc:
                    raise ProductCompositionError(
                        f"product source authority field {name!r} disappeared after composition"
                    ) from exc
                if current is not expected:
                    raise ProductCompositionError(
                        f"product source authority field {name!r} changed after composition"
                    )

        def require_source_resolver_authority() -> None:
            require_declared_source_authority_roots()
            current_identity = _source_resolver_identity_fn(
                source=source,
                source_id=source_id,
            )
            if current_identity != manifest.source_resolver_identity:
                raise ProductCompositionError(
                    "source resolver authority changed after product composition"
                )
            require_declared_source_authority_roots()

        def build_sealed_product_proxy_type(
            type_name: str,
            *,
            methods: dict[str, Callable],
            properties: dict[str, Callable[[], object]] | None = None,
            authority_label: str,
        ):
            property_getters = {} if properties is None else dict(properties)

            class ProxyMethod:
                __slots__ = ("method", "name")

                def __init__(self, name: str, method: Callable) -> None:
                    self.name = name
                    self.method = method

                def __get__(self, instance, owner=None):
                    if instance is None:
                        return self
                    return self.method.__get__(instance, owner)

                def __set__(self, _instance, _value) -> None:
                    raise ProductCompositionError(
                        f"{authority_label} proxy method {self.name!r} is immutable"
                    )

                def __delete__(self, _instance) -> None:
                    raise ProductCompositionError(
                        f"{authority_label} proxy method {self.name!r} is immutable"
                    )

            class ProxyProperty:
                __slots__ = ("getter", "name")

                def __init__(self, name: str, getter: Callable[[], object]) -> None:
                    self.name = name
                    self.getter = getter

                def __get__(self, instance, owner=None):
                    if instance is None:
                        return self
                    return self.getter()

                def __set__(self, _instance, _value) -> None:
                    raise ProductCompositionError(
                        f"{authority_label} proxy property {self.name!r} is immutable"
                    )

                def __delete__(self, _instance) -> None:
                    raise ProductCompositionError(
                        f"{authority_label} proxy property {self.name!r} is immutable"
                    )

            class ProxyClassIdentity:
                __slots__ = ()

                def __get__(self, _instance, owner=None):
                    return owner

                def __set__(self, _instance, _value) -> None:
                    raise ProductCompositionError(
                        f"{authority_label} proxy class identity is immutable"
                    )

                def __delete__(self, _instance) -> None:
                    raise ProductCompositionError(
                        f"{authority_label} proxy class identity is immutable"
                    )

            class ProxyClassGuard:
                __slots__ = ("descriptor", "name")

                def __init__(self, name: str, descriptor: object) -> None:
                    self.name = name
                    self.descriptor = descriptor

                def __get__(self, _instance, _owner=None):
                    return self.descriptor

                def __set__(self, _instance, _value) -> None:
                    raise ProductCompositionError(
                        f"{authority_label} proxy class member {self.name!r} is immutable"
                    )

                def __delete__(self, _instance) -> None:
                    raise ProductCompositionError(
                        f"{authority_label} proxy class member {self.name!r} is immutable"
                    )

            class ProxyMeta(type):
                pass

            namespace: dict[str, object] = {
                "__slots__": (),
                "__class__": ProxyClassIdentity(),
            }
            protected: dict[str, object] = {}
            for name, method in methods.items():
                descriptor = ProxyMethod(name, method)
                namespace[name] = descriptor
                protected[name] = descriptor
            for name, getter in property_getters.items():
                descriptor = ProxyProperty(name, getter)
                namespace[name] = descriptor
                protected[name] = descriptor

            proxy_type = ProxyMeta(type_name, (), namespace)
            for name, descriptor in protected.items():
                type.__setattr__(
                    ProxyMeta,
                    name,
                    ProxyClassGuard(name, descriptor),
                )
            return proxy_type

        product_outcome_authority: SettlementOutcomeAuthority | None = None
        if outcome_authority is not None:
            source_settlement_resolve = source.resolve

            def require_settlement_authority() -> None:
                require_declared_source_authority_roots()
                current_identity = _settlement_authority_identity_fn(
                    source=source,
                    source_id=source_id,
                    outcome_authority=source,
                )
                if current_identity != manifest.settlement_authority_identity:
                    raise ProductCompositionError(
                        "settlement authority changed after product composition"
                    )
                require_declared_source_authority_roots()

            def product_settlement_resolve(_proxy, record, *, as_of: str):
                require_settlement_authority()
                resolution = source_settlement_resolve(record, as_of=as_of)
                require_settlement_authority()
                return resolution

            ProductSettlementOutcomeAuthorityProxy = build_sealed_product_proxy_type(
                "ProductSettlementOutcomeAuthorityProxy",
                methods={"resolve": product_settlement_resolve},
                authority_label="settlement outcome",
            )
            product_outcome_authority = ProductSettlementOutcomeAuthorityProxy()

        product_settlement_learning_handoff: SettlementLearningHandoff | None = None
        if settlement_learning_handoff is not None:
            declared_learning_authority_fields = getattr(
                type(settlement_learning_handoff),
                "_AUTHORITY_FIELDS",
                frozenset(),
            )
            if type(declared_learning_authority_fields) is not frozenset or any(
                type(name) is not str or not name
                for name in declared_learning_authority_fields
            ):
                raise ProductCompositionError(
                    "settlement learning _AUTHORITY_FIELDS must be a frozenset of non-empty strings"
                )
            learning_authority_snapshot: tuple[tuple[str, object], ...] = tuple(
                (
                    name,
                    object.__getattribute__(settlement_learning_handoff, name),
                )
                for name in sorted(declared_learning_authority_fields)
            )
            learning_prepare = getattr(
                settlement_learning_handoff,
                "prepare_settlement",
                None,
            )
            learning_reconcile = getattr(
                settlement_learning_handoff,
                "reconcile_after_settlement",
                None,
            )
            if learning_prepare is not None and not callable(learning_prepare):
                raise ProductCompositionError(
                    "settlement learning prepare_settlement must be callable or absent"
                )
            if not callable(learning_reconcile):
                raise ProductCompositionError(
                    "settlement learning reconcile_after_settlement must be callable"
                )

            def require_settlement_learning_authority() -> None:
                current_identity = _settlement_learning_handoff_identity_fn(
                    handoff=settlement_learning_handoff,
                )
                if (
                    current_identity
                    != manifest.settlement_learning_handoff_identity
                ):
                    raise ProductCompositionError(
                        "settlement learning handoff authority changed after product composition"
                    )
                for name, expected in learning_authority_snapshot:
                    try:
                        current = object.__getattribute__(
                            settlement_learning_handoff,
                            name,
                        )
                    except AttributeError as exc:
                        raise ProductCompositionError(
                            f"settlement learning authority field {name!r} disappeared after composition"
                        ) from exc
                    if current is not expected:
                        raise ProductCompositionError(
                            f"settlement learning authority field {name!r} changed after composition"
                        )

            def product_learning_reconcile(
                _proxy,
                *,
                paper_book_path,
                resolutions,
                settled_ticket_ids,
                at,
            ):
                require_settlement_learning_authority()
                result = learning_reconcile(
                    paper_book_path=paper_book_path,
                    resolutions=resolutions,
                    settled_ticket_ids=settled_ticket_ids,
                    at=at,
                )
                require_settlement_learning_authority()
                return result

            learning_proxy_methods = {
                "reconcile_after_settlement": product_learning_reconcile,
            }
            if learning_prepare is not None:
                def product_learning_prepare(
                    _proxy,
                    *,
                    paper_book_path,
                    resolutions,
                    at,
                ):
                    require_settlement_learning_authority()
                    result = learning_prepare(
                        paper_book_path=paper_book_path,
                        resolutions=resolutions,
                        at=at,
                    )
                    require_settlement_learning_authority()
                    return result

                learning_proxy_methods["prepare_settlement"] = product_learning_prepare

            ProductSettlementLearningHandoffProxy = build_sealed_product_proxy_type(
                "ProductSettlementLearningHandoffProxy",
                methods=learning_proxy_methods,
                authority_label="settlement learning",
            )
            product_settlement_learning_handoff = ProductSettlementLearningHandoffProxy()

        source_fetch_catalog_page = source.fetch_catalog_page
        source_fetch_deltas = source.fetch_deltas

        def product_source_id() -> str:
            return source_id

        def product_stream_epoch() -> str:
            return getattr(source, "stream_epoch")

        def product_fetch_catalog_page(proxy, checkpoint):
            expected_stream_epoch = proxy.stream_epoch
            require_source_resolver_authority()
            page = source_fetch_catalog_page(checkpoint)
            require_source_resolver_authority()
            if proxy.stream_epoch != expected_stream_epoch:
                raise ProductCompositionError(
                    "source stream_epoch changed during catalog acquisition"
                )
            return page

        def product_fetch_deltas(proxy, checkpoint, records, max_items):
            expected_stream_epoch = proxy.stream_epoch
            require_source_resolver_authority()
            deltas = source_fetch_deltas(checkpoint, records, max_items)
            require_source_resolver_authority()
            if proxy.stream_epoch != expected_stream_epoch:
                raise ProductCompositionError(
                    "source stream_epoch changed during delta acquisition"
                )
            return deltas

        ProductCollectorSourceProxy = build_sealed_product_proxy_type(
            "ProductCollectorSourceProxy",
            methods={
                "fetch_catalog_page": product_fetch_catalog_page,
                "fetch_deltas": product_fetch_deltas,
            },
            properties={
                "source_id": product_source_id,
                "stream_epoch": product_stream_epoch,
            },
            authority_label="collector source",
        )
        collector_source = ProductCollectorSourceProxy()

        lifecycle = object.__new__(_lifecycle_type)
        _lifecycle_init(lifecycle, root / "catalog.json")
        market_store = object.__new__(_market_store_type)
        _market_store_init(market_store, root / "market.db")
        lease_stack.callback(_market_store_close, market_store)
        mirror = object.__new__(_mirror_type)
        _mirror_init(mirror)
        invalidations = object.__new__(_invalidation_buffer_type)
        _invalidation_buffer_init(invalidations, mirror)

        market_bus = object.__new__(_market_bus_type)
        _market_bus_init(market_bus, market_store)
        source_health = object.__new__(_source_health_type)
        _source_health_init(source_health, root / "source_health.json")
        canonical_application = object.__new__(_canonical_application_type)
        _canonical_application_init(
            canonical_application,
            market_bus,
            source_health,
            root / "desktop_application.json",
            clock=resolved_clock,
        )
        # Restart decision state belongs to the collector/DesktopApplicationReceipt
        # authority family. Completed application evidence survives lawful collector
        # compaction. Generic/import rows remain canonical audit history, but neither
        # another source nor an unreceipted row from this source may seed the
        # autonomous decision mirror. A newer generic row also cannot hide an older,
        # exact desktop-applied row for the same quote.
        for event in _desktop_restart_reader(
            source_id=source_id,
            market_store=market_store,
            canonical_application=canonical_application,
        ):
            invalidations.accept_persisted(event)

        dependencies = object.__new__(_dependency_index_type)
        _dependency_index_init(dependencies, mirror)
        collector_store = object.__new__(_collector_store_type)
        _collector_store_init(collector_store, root / "collector_deltas.json")

        from weakref import WeakKeyDictionary

        collector_snapshots = WeakKeyDictionary()
        collector_snapshot_fields = (
            "delta_store",
            "lifecycle",
            "source",
            "_source_identity",
            "_source_id",
            "config",
            "clock",
            "sleep",
            "random_value",
            "stop_requested",
            "stop_reason",
            "_adapter",
            "_state",
        )
        base_collector_init = _collector_service_init
        base_collector_status = _collector_service_status
        base_collector_resume = _collector_service_resume
        base_collector_stop = _collector_service_stop
        base_collector_run_cycle = _collector_service_run_cycle
        base_bounded_provider_call = _collector_service_bounded_provider_call
        collector_entry_names = frozenset(
            {"status", "resume", "stop", "run_cycle", "_bounded_provider_call"}
        )
        collector_missing = object()

        def require_collector_authority(self) -> None:
            snapshot = collector_snapshots.get(self)
            if snapshot is None:
                raise ProductCompositionError(
                    "product collector authority snapshot is unavailable"
                )
            raw = object.__getattribute__(self, "__dict__")
            for name in collector_entry_names:
                if name in raw:
                    raise ProductCompositionError(
                        f"product collector method {name!r} changed after composition"
                    )
            for name, expected in snapshot:
                if raw.get(name, collector_missing) is not expected:
                    raise ProductCompositionError(
                        f"product collector authority field {name!r} changed after composition"
                    )

        def product_collector_status(self):
            require_collector_authority(self)
            return base_collector_status(self)

        def product_collector_resume(self):
            require_collector_authority(self)
            result = base_collector_resume(self)
            require_collector_authority(self)
            return result

        def product_collector_stop(self, reason="operator_stop"):
            require_collector_authority(self)
            result = base_collector_stop(self, reason)
            require_collector_authority(self)
            return result

        def product_bounded_provider_call(self, action):
            require_collector_authority(self)

            def guarded_action():
                require_collector_authority(self)
                require_source_resolver_authority()
                result = action()
                require_source_resolver_authority()
                require_collector_authority(self)
                return result

            result = base_bounded_provider_call(self, guarded_action)
            require_collector_authority(self)
            return result

        def product_collector_run_cycle(self, *, _schedule_slot=None):
            require_collector_authority(self)
            require_source_resolver_authority()
            result = base_collector_run_cycle(
                self,
                _schedule_slot=_schedule_slot,
            )
            require_source_resolver_authority()
            require_collector_authority(self)
            return result

        collector_entries = {
            "status": product_collector_status,
            "resume": product_collector_resume,
            "stop": product_collector_stop,
            "run_cycle": product_collector_run_cycle,
            "_bounded_provider_call": product_bounded_provider_call,
        }

        class ProductCollectorClassIdentity:
            __slots__ = ()

            def __get__(self, _instance, owner=None):
                return owner

            def __set__(self, _instance, _value) -> None:
                raise ProductCompositionError(
                    "product collector class identity is immutable"
                )

            def __delete__(self, _instance) -> None:
                raise ProductCompositionError(
                    "product collector class identity is immutable"
                )

        class ProductCollectorClassGuard:
            __slots__ = ("descriptor", "name")

            def __init__(self, name: str, descriptor: object) -> None:
                self.name = name
                self.descriptor = descriptor

            def __get__(self, _instance, _owner=None):
                return self.descriptor

            def __set__(self, _instance, _value) -> None:
                raise ProductCompositionError(
                    f"product collector class member {self.name!r} is immutable"
                )

            def __delete__(self, _instance) -> None:
                raise ProductCompositionError(
                    f"product collector class member {self.name!r} is immutable"
                )

        class ProductCollectorMeta(type(_collector_service_type)):
            pass

        class ProductCollectorService(
            _collector_service_type,
            metaclass=ProductCollectorMeta,
        ):
            __class__ = ProductCollectorClassIdentity()
            def __init__(self, *args, **kwargs) -> None:
                base_collector_init(self, *args, **kwargs)
                collector_snapshots[self] = tuple(
                    (name, object.__getattribute__(self, name))
                    for name in collector_snapshot_fields
                )

            def __getattribute__(self, name: str):
                snapshot = collector_snapshots.get(self)
                if snapshot is not None:
                    if name in collector_entries:
                        require_collector_authority(self)
                        return collector_entries[name].__get__(self, type(self))
                    if name in collector_snapshot_fields:
                        for field_name, expected in snapshot:
                            if field_name == name:
                                return expected
                return object.__getattribute__(self, name)

            def __setattr__(self, name: str, value: object) -> None:
                if collector_snapshots.get(self) is not None and (
                    name in collector_snapshot_fields
                    or name in collector_entry_names
                    or name in {"__class__", "__dict__"}
                ):
                    raise ProductCompositionError(
                        f"product collector authority field {name!r} is immutable"
                    )
                object.__setattr__(self, name, value)

        for name in ("__getattribute__", "__setattr__"):
            descriptor = ProductCollectorService.__dict__[name]
            type.__setattr__(
                ProductCollectorMeta,
                name,
                ProductCollectorClassGuard(name, descriptor),
            )

        collector = ProductCollectorService(
            delta_store=collector_store,
            lifecycle=lifecycle,
            source=collector_source,
            state_path=root / "collector_state.json",
            run_id=f"product:{source_id}",
            clock=resolved_clock,
            sleep=sleep,
        )

        accept_persisted = invalidations.accept_persisted
        source_resolve_event = source.resolve_event

        def resolve_product_event(delta: CollectorDelta) -> MarketEvent:
            require_source_resolver_authority()
            event = source_resolve_event(delta)
            require_source_resolver_authority()
            return event

        def lookup_completed_desktop_application(
            delta: CollectorDelta,
        ) -> DesktopApplicationReceipt | None:
            return _canonical_application_lookup(canonical_application, delta)

        def apply_completed_desktop_application(
            delta: CollectorDelta,
            event: MarketEvent,
        ) -> DesktopApplicationReceipt:
            receipt = _canonical_application_apply(
                canonical_application,
                delta,
                event,
            )
            if type(receipt) is not DesktopApplicationReceipt:
                raise ProductCompositionError(
                    "canonical desktop application returned a non-canonical receipt"
                )
            recovered = lookup_completed_desktop_application(delta)
            if recovered is None:
                raise ProductCompositionError(
                    "fresh canonical desktop application receipt is not durably recoverable"
                )
            if type(recovered) is not DesktopApplicationReceipt:
                raise ProductCompositionError(
                    "recovered canonical desktop application receipt type is invalid"
                )
            if (
                recovered.delta_id != receipt.delta_id
                or recovered.canonical_event_digest
                != receipt.canonical_event_digest
                or recovered.receipt_id != receipt.receipt_id
                or recovered.applied_at != receipt.applied_at
            ):
                raise ProductCompositionError(
                    "fresh canonical desktop application receipt changed during durable recovery"
                )
            return receipt

        def deliver_completed_desktop_application(
            delta: CollectorDelta,
            receipt: DesktopApplicationReceipt,
        ) -> None:
            event = _desktop_delivery_resolver(
                source_id=source_id,
                delta=delta,
                receipt=receipt,
                market_store=market_store,
            )
            accept_persisted(event)

        checkpoint = object.__new__(_checkpoint_type)
        _checkpoint_init(checkpoint, root / "desktop_acks.json")
        desktop = object.__new__(_desktop_consumer_type)
        _desktop_consumer_init(
            desktop,
            collector_store,
            checkpoint,
            resolve_event=resolve_product_event,
            apply_event=apply_completed_desktop_application,
            lookup_application_receipt=lookup_completed_desktop_application,
            acknowledgement_clock=resolved_clock,
            on_application_receipt=deliver_completed_desktop_application,
        )
        coordinator = object.__new__(_coordinator_type)
        _coordinator_init(
            coordinator,
            workspace=root,
            collector=collector,
            lifecycle=lifecycle,
            market_store=market_store,
            desktop_consumer=desktop,
            invalidation_buffer=invalidations,
            dependency_index=dependencies,
            outcome_authority=product_outcome_authority,
            settlement_learning_handoff=product_settlement_learning_handoff,
            clock=resolved_clock,
            initial_bankroll=manifest.initial_bankroll,
        )
        raw_start_transition_store = object.__new__(
            _start_transition_store_type
        )
        _start_transition_store_init(
            raw_start_transition_store,
            root / "product_start_transition.json",
        )
        start_transition_path = object.__getattribute__(
            raw_start_transition_store,
            "path",
        )
        pending_start_phases = frozenset({"STARTING", "RECOVERY_REQUIRED"})

        def read_product_start_transition():
            return _start_transition_read(raw_start_transition_store)

        def write_product_start_transition(payload):
            _start_transition_write(start_transition_path, payload)

        def product_start_pending(_proxy):
            raw = read_product_start_transition()
            if raw is None or raw["phase"] not in pending_start_phases:
                return None
            return dict(raw)

        def product_start_begin(
            _proxy,
            *,
            collector_was_stopped,
            session_pre_state,
        ):
            if type(collector_was_stopped) is not bool:
                raise ProductCompositionError(
                    "product START collector pre-state must be boolean"
                )
            if session_pre_state not in {"RUNNING", "PAUSED", "STOPPED"}:
                raise ProductCompositionError(
                    "product START session pre-state is invalid"
                )
            if collector_was_stopped != (session_pre_state == "STOPPED"):
                raise ProductCompositionError(
                    "product START pre-state authorities disagree"
                )
            current = read_product_start_transition()
            if current is not None and current["phase"] in pending_start_phases:
                raise ProductCompositionError(
                    "unfinished product START transition requires recovery"
                )
            generation = 1 if current is None else int(current["generation"]) + 1
            write_product_start_transition(
                {
                    "schema": "autosport.product_runtime_start_transition",
                    "schema_version": 1,
                    "generation": generation,
                    "phase": "STARTING",
                    "collector_was_stopped": collector_was_stopped,
                    "session_pre_state": session_pre_state,
                }
            )
            verified = read_product_start_transition()
            if (
                verified is None
                or verified["generation"] != generation
                or verified["phase"] != "STARTING"
            ):
                raise ProductCompositionError(
                    "durable product START transition publication could not be verified"
                )
            return generation

        def mark_product_start_transition(
            generation,
            phase,
            *,
            allowed_from,
        ):
            current = read_product_start_transition()
            if current is None or current["generation"] != generation:
                raise ProductCompositionError(
                    "durable product START transition generation changed"
                )
            current_phase = current["phase"]
            if current_phase == phase:
                return
            if current_phase not in allowed_from:
                raise ProductCompositionError(
                    "durable product START transition phase changed unexpectedly"
                )
            updated = dict(current)
            updated["phase"] = phase
            write_product_start_transition(updated)
            verified = read_product_start_transition()
            if (
                verified is None
                or verified["generation"] != generation
                or verified["phase"] != phase
            ):
                raise ProductCompositionError(
                    "durable product START transition update could not be verified"
                )

        def product_start_mark_completed(_proxy, generation):
            mark_product_start_transition(
                generation,
                "COMPLETED",
                allowed_from=frozenset({"STARTING"}),
            )

        def product_start_mark_rolled_back(_proxy, generation):
            mark_product_start_transition(
                generation,
                "ROLLED_BACK",
                allowed_from=frozenset({"STARTING", "RECOVERY_REQUIRED"}),
            )

        def product_start_mark_recovery_required(_proxy, generation):
            mark_product_start_transition(
                generation,
                "RECOVERY_REQUIRED",
                allowed_from=frozenset({"STARTING", "RECOVERY_REQUIRED"}),
            )

        ProductStartTransitionStoreProxy = build_sealed_product_proxy_type(
            "ProductStartTransitionStoreProxy",
            methods={
                "pending": product_start_pending,
                "begin": product_start_begin,
                "mark_completed": product_start_mark_completed,
                "mark_rolled_back": product_start_mark_rolled_back,
                "mark_recovery_required": product_start_mark_recovery_required,
            },
            authority_label="START transition",
        )
        product_start_transition_store = ProductStartTransitionStoreProxy()

        def product_runtime_lease_release(_proxy):
            return _runtime_lease_release(raw_runtime_lease)

        def product_runtime_lease_bind_operation_fence(_proxy, operation_fence):
            return _runtime_lease_bind_operation_fence(
                raw_runtime_lease,
                operation_fence,
            )

        def product_runtime_lease_authority_active():
            return _runtime_lease_authority_active_getter(raw_runtime_lease)

        ProductRuntimeLeaseProxy = build_sealed_product_proxy_type(
            "ProductRuntimeLeaseProxy",
            methods={
                "release": product_runtime_lease_release,
                "bind_operation_fence": product_runtime_lease_bind_operation_fence,
            },
            properties={
                "authority_active": product_runtime_lease_authority_active,
            },
            authority_label="runtime lease",
        )
        product_runtime_lease = ProductRuntimeLeaseProxy()

        runtime = object.__new__(_runtime_type)
        _runtime_init(
            runtime,
            workspace=root,
            manifest=manifest,
            coordinator=coordinator,
            collector=collector,
            market_store=market_store,
            lifecycle=lifecycle,
            mirror=mirror,
            invalidations=invalidations,
            dependencies=dependencies,
            _runtime_lease=product_runtime_lease,
            _start_transition_store=product_start_transition_store,
        )
        product_runtime_lease.bind_operation_fence(runtime._operation_fence)
        runtime._recover_interrupted_start()
        lease_stack.pop_all()
        return runtime


def _bind_autonomous_product_runtime_builder(
    implementation,
    desktop_restart_reader,
    desktop_delivery_resolver,
    desktop_consumer_type,
    desktop_consumer_init,
    canonical_application_type,
    canonical_application_init,
    canonical_application_apply,
    canonical_application_lookup,
    paper_book_type,
    paper_book_init,
    runtime_type,
    runtime_init,
    runtime_lease_type,
    runtime_lease_init,
    runtime_lease_acquire,
    runtime_lease_release,
    runtime_lease_bind_operation_fence,
    runtime_lease_authority_active_getter,
    source_resolver_identity_fn,
    settlement_authority_identity_fn,
    settlement_learning_handoff_identity_fn,
    manifest_store_type,
    manifest_store_init,
    manifest_store_load_or_create,
    manifest_store_read_raw,
    manifest_loads,
    manifest_write,
    manifest_text_validator,
    manifest_type,
    lifecycle_type,
    lifecycle_init,
    market_store_type,
    market_store_init,
    market_store_close,
    mirror_type,
    mirror_init,
    invalidation_buffer_type,
    invalidation_buffer_init,
    market_bus_type,
    market_bus_init,
    source_health_type,
    source_health_init,
    dependency_index_type,
    dependency_index_init,
    collector_store_type,
    collector_store_init,
    collector_service_type,
    collector_service_init,
    collector_service_status,
    collector_service_resume,
    collector_service_stop,
    collector_service_run_cycle,
    collector_service_bounded_provider_call,
    checkpoint_type,
    checkpoint_init,
    coordinator_type,
    coordinator_init,
    start_transition_store_type,
    start_transition_store_init,
    start_transition_read,
    start_transition_write,
):
    """Expose the product builder without mutable restart/delivery dispatch."""

    def build_autonomous_product_runtime(
        *,
        workspace: str | Path,
        source: ProductCollectorSource,
        clock: Callable[[], str] | None = None,
        sleep: Callable[[float], None] | None = None,
        initial_bankroll: str = "10000",
        outcome_authority: SettlementOutcomeAuthority | None = None,
        settlement_learning_handoff: SettlementLearningHandoff | None = None,
    ) -> AutonomousProductRuntime:
        return implementation(
            workspace=workspace,
            source=source,
            clock=clock,
            sleep=sleep,
            initial_bankroll=initial_bankroll,
            outcome_authority=outcome_authority,
            settlement_learning_handoff=settlement_learning_handoff,
            _desktop_restart_reader=desktop_restart_reader,
            _desktop_delivery_resolver=desktop_delivery_resolver,
            _desktop_consumer_type=desktop_consumer_type,
            _desktop_consumer_init=desktop_consumer_init,
            _canonical_application_type=canonical_application_type,
            _canonical_application_init=canonical_application_init,
            _canonical_application_apply=canonical_application_apply,
            _canonical_application_lookup=canonical_application_lookup,
            _paper_book_type=paper_book_type,
            _paper_book_init=paper_book_init,
            _runtime_type=runtime_type,
            _runtime_init=runtime_init,
            _runtime_lease_type=runtime_lease_type,
            _runtime_lease_init=runtime_lease_init,
            _runtime_lease_acquire=runtime_lease_acquire,
            _runtime_lease_release=runtime_lease_release,
            _runtime_lease_bind_operation_fence=runtime_lease_bind_operation_fence,
            _runtime_lease_authority_active_getter=runtime_lease_authority_active_getter,
            _source_resolver_identity_fn=source_resolver_identity_fn,
            _settlement_authority_identity_fn=settlement_authority_identity_fn,
            _settlement_learning_handoff_identity_fn=settlement_learning_handoff_identity_fn,
            _manifest_store_type=manifest_store_type,
            _manifest_store_init=manifest_store_init,
            _manifest_store_load_or_create=manifest_store_load_or_create,
            _manifest_store_read_raw=manifest_store_read_raw,
            _manifest_loads=manifest_loads,
            _manifest_write=manifest_write,
            _manifest_text_validator=manifest_text_validator,
            _manifest_type=manifest_type,
            _lifecycle_type=lifecycle_type,
            _lifecycle_init=lifecycle_init,
            _market_store_type=market_store_type,
            _market_store_init=market_store_init,
            _market_store_close=market_store_close,
            _mirror_type=mirror_type,
            _mirror_init=mirror_init,
            _invalidation_buffer_type=invalidation_buffer_type,
            _invalidation_buffer_init=invalidation_buffer_init,
            _market_bus_type=market_bus_type,
            _market_bus_init=market_bus_init,
            _source_health_type=source_health_type,
            _source_health_init=source_health_init,
            _dependency_index_type=dependency_index_type,
            _dependency_index_init=dependency_index_init,
            _collector_store_type=collector_store_type,
            _collector_store_init=collector_store_init,
            _collector_service_type=collector_service_type,
            _collector_service_init=collector_service_init,
            _collector_service_status=collector_service_status,
            _collector_service_resume=collector_service_resume,
            _collector_service_stop=collector_service_stop,
            _collector_service_run_cycle=collector_service_run_cycle,
            _collector_service_bounded_provider_call=collector_service_bounded_provider_call,
            _checkpoint_type=checkpoint_type,
            _checkpoint_init=checkpoint_init,
            _coordinator_type=coordinator_type,
            _coordinator_init=coordinator_init,
            _start_transition_store_type=start_transition_store_type,
            _start_transition_store_init=start_transition_store_init,
            _start_transition_read=start_transition_read,
            _start_transition_write=start_transition_write,
        )

    build_autonomous_product_runtime.__doc__ = implementation.__doc__
    return build_autonomous_product_runtime


build_autonomous_product_runtime = _bind_autonomous_product_runtime_builder(
    _build_autonomous_product_runtime_impl,
    _desktop_applied_current_for_source,
    _desktop_applied_event_for_receipt,
    _ProductDesktopDeltaConsumer,
    _ProductDesktopDeltaConsumer.__init__,
    CanonicalDesktopApplication,
    CanonicalDesktopApplication.__init__,
    CanonicalDesktopApplication.apply,
    CanonicalDesktopApplication.lookup_receipt,
    PaperBook,
    PaperBook.__init__,
    _ProductAutonomousProductRuntime,
    _ProductAutonomousProductRuntime.__init__,
    _ProductRuntimeLease,
    _product_runtime_lease_init,
    _product_runtime_lease_acquire,
    _product_runtime_lease_release,
    _product_runtime_lease_bind_operation_fence,
    _product_runtime_lease_authority_active,
    _source_resolver_identity,
    _settlement_authority_identity,
    _settlement_learning_handoff_identity,
    _ManifestStore,
    _ManifestStore.__init__,
    _ManifestStore.load_or_create,
    _ManifestStore._read_raw,
    strict_json_loads,
    atomic_write_json,
    _ManifestStore._text,
    ProductCompositionManifest,
    ContinuousEventLifecycle,
    ContinuousEventLifecycle.__init__,
    SQLiteMarketStore,
    SQLiteMarketStore.__init__,
    SQLiteMarketStore.close,
    MarketMirror,
    MarketMirror.__init__,
    BoundedMirrorInvalidationBuffer,
    BoundedMirrorInvalidationBuffer.__init__,
    MarketEventBus,
    MarketEventBus.__init__,
    SourceHealthStore,
    SourceHealthStore.__init__,
    FocusedMirrorDependencyIndex,
    FocusedMirrorDependencyIndex.__init__,
    CollectorDeltaStore,
    CollectorDeltaStore.__init__,
    HeadlessCollectorService,
    HeadlessCollectorService.__init__,
    HeadlessCollectorService.status,
    HeadlessCollectorService.resume,
    HeadlessCollectorService.stop,
    HeadlessCollectorService.run_cycle,
    HeadlessCollectorService._bounded_provider_call,
    DesktopDeltaCheckpointStore,
    DesktopDeltaCheckpointStore.__init__,
    _ProductContinuousSessionCoordinator,
    _ProductContinuousSessionCoordinator.__init__,
    _ProductStartTransitionStore,
    _ProductStartTransitionStore.__init__,
    _read_product_start_transition,
    atomic_write_json,
)
del _ProductDesktopDeltaConsumer
del _ProductContinuousSessionCoordinator
del _ProductAutonomousProductRuntime
del _build_autonomous_product_runtime_impl
del _desktop_applied_current_for_source
del _desktop_applied_event_for_receipt
del _bind_autonomous_product_runtime_builder
