from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from types import FunctionType
from typing import Callable, Protocol

from .causal_collector import (
    CanonicalDesktopApplication,
    CollectorDelta,
    CollectorDeltaStore,
    DesktopDeltaCheckpointStore,
    DesktopDeltaConsumer,
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

    def __init__(self, workspace: str | Path) -> None:
        super().__init__(workspace)
        self._authority_active = False
        self._acquired_once = False

    @property
    def authority_active(self) -> bool:
        """Whether this one-shot lease still grants positive runtime authority."""
        return self._authority_active

    def acquire(self) -> None:
        # A product runtime lease is a one-shot lifetime capability. Reacquiring the
        # same mutable lock object after release could resurrect an old runtime object
        # after ownership has moved elsewhere.
        if self._acquired_once:
            raise WorkspaceEconomicLockError(
                "product runtime workspace authority cannot be reacquired"
            )
        super().acquire()
        self._acquired_once = True
        self._authority_active = True

    def release(self) -> None:
        # Revoke product authority before attempting OS teardown. Even if unlock/close
        # later reports an error, callers must never treat ownership as positively held.
        self._authority_active = False
        super().release()


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
        if not self.path.exists():
            return None
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ProductCompositionError(
                "cannot verify durable product START transition"
            ) from exc
        if (
            type(raw) is not dict
            or set(raw) != self._FIELDS
            or raw.get("schema") != self._SCHEMA
            or raw.get("schema_version") != self._VERSION
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
        if phase not in self._PHASES:
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
        if raw is None or raw["phase"] not in self._PENDING_PHASES:
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
        if current is not None and current["phase"] in self._PENDING_PHASES:
            raise ProductCompositionError(
                "unfinished product START transition requires recovery"
            )
        generation = 1 if current is None else int(current["generation"]) + 1
        atomic_write_json(
            self.path,
            {
                "schema": self._SCHEMA,
                "schema_version": self._VERSION,
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


class _ManifestStore:
    _SCHEMA = "autosport.autonomous_product_composition"
    _VERSION = 2
    _V1_FIELDS = {"schema", "schema_version", "source_id", "initial_bankroll"}
    _FIELDS = {
        "schema",
        "schema_version",
        "source_id",
        "initial_bankroll",
        "settlement_authority_identity",
    }

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def _text(value: object, field: str) -> str:
        if type(value) is not str or not value or value.strip() != value:
            raise ProductCompositionError(f"{field} must be a non-empty trimmed string")
        return value

    def _read_raw(self) -> dict[str, object]:
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ProductCompositionError("cannot verify product composition manifest") from exc
        if type(raw) is not dict or raw.get("schema") != self._SCHEMA:
            raise ProductCompositionError("product composition manifest schema mismatch")
        version = raw.get("schema_version")
        if version == 1 and set(raw) == self._V1_FIELDS:
            self._text(raw.get("source_id"), "source_id")
            self._text(raw.get("initial_bankroll"), "initial_bankroll")
            return {
                **raw,
                "settlement_authority_identity": None,
            }
        if version != self._VERSION or set(raw) != self._FIELDS:
            raise ProductCompositionError("product composition manifest schema mismatch")
        self._text(raw.get("source_id"), "source_id")
        self._text(raw.get("initial_bankroll"), "initial_bankroll")
        authority_identity = raw.get("settlement_authority_identity")
        if authority_identity is not None:
            identity = self._text(
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
        return raw

    def load_or_create(
        self,
        *,
        source_id: str,
        initial_bankroll: str,
        settlement_authority_identity: str | None,
    ) -> ProductCompositionManifest:
        source_id = self._text(source_id, "source_id")
        initial_bankroll = self._text(initial_bankroll, "initial_bankroll")
        if settlement_authority_identity is not None:
            settlement_authority_identity = self._text(
                settlement_authority_identity,
                "settlement_authority_identity",
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            atomic_write_json(
                self.path,
                {
                    "schema": self._SCHEMA,
                    "schema_version": self._VERSION,
                    "source_id": source_id,
                    "initial_bankroll": initial_bankroll,
                    "settlement_authority_identity": settlement_authority_identity,
                },
            )
        raw = self._read_raw()
        if raw["source_id"] != source_id:
            raise ProductCompositionError(
                "configured source_id conflicts with durable product composition"
            )
        if raw["initial_bankroll"] != initial_bankroll:
            raise ProductCompositionError(
                "configured initial_bankroll conflicts with durable product composition"
            )
        if raw["settlement_authority_identity"] != settlement_authority_identity:
            raise ProductCompositionError(
                "settlement authority identity conflicts with durable product composition"
            )
        return ProductCompositionManifest(
            source_id=source_id,
            initial_bankroll=normalized_bankroll,
            settlement_authority_identity=settlement_authority_identity,
        )


def _settlement_authority_identity(
    *,
    source: ProductCollectorSource,
    source_id: str,
    outcome_authority: SettlementOutcomeAuthority | None,
) -> str | None:
    if outcome_authority is None:
        return None
    if outcome_authority is not source:
        raise ProductCompositionError(
            "settlement outcome authority must be owned by the configured product source"
        )
    instance_dict = getattr(source, "__dict__", None)
    if type(instance_dict) is dict and "resolve" in instance_dict:
        raise ProductCompositionError(
            "source-owned settlement authority forbids per-instance resolve shadowing"
        )
    resolver = getattr(type(source), "resolve", None)
    if type(resolver) is not FunctionType:
        raise ProductCompositionError(
            "source-owned settlement authority must use a concrete class resolve method"
        )
    if resolver.__defaults__ is not None or resolver.__kwdefaults__ not in (None, {}):
        raise ProductCompositionError(
            "source-owned settlement resolve method cannot use mutable call defaults"
        )
    if resolver.__closure__ is not None:
        raise ProductCompositionError(
            "source-owned settlement resolve method cannot close over mutable authority"
        )
    try:
        resolver_semantic_sha256 = function_semantic_sha256(
            resolver,
            runtime_owner=type(source),
        )
    except ResolverSemanticIdentityError as exc:
        raise ProductCompositionError(
            "source-owned settlement resolve semantics cannot be fingerprinted safely"
        ) from exc
    resolver_owner = _ManifestStore._text(
        f"{resolver.__module__}.{resolver.__qualname__}",
        "settlement resolver owner",
    )
    declared_implementation_id = getattr(
        type(source),
        "settlement_resolver_implementation_id",
        None,
    )
    if declared_implementation_id is None:
        raise ProductCompositionError(
            "source-owned settlement authority must declare stable settlement_resolver_implementation_id"
        )
    if (
        type(instance_dict) is dict
        and "settlement_resolver_implementation_id" in instance_dict
    ):
        raise ProductCompositionError(
            "source-owned settlement authority forbids per-instance implementation identity shadowing"
        )
    resolver_implementation_id = _ManifestStore._text(
        declared_implementation_id,
        "settlement_resolver_implementation_id",
    )
    authority_id = _ManifestStore._text(
        getattr(source, "settlement_authority_id", None),
        "settlement_authority_id",
    )
    configuration_sha256 = _ManifestStore._text(
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
        raise ProductCompositionError(
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
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
        self._closed = True
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
        PaperBook(normalized_bankroll)
    except Exception as exc:
        raise ValueError("initial_bankroll must construct a valid PaperBook") from exc

    root.mkdir(parents=True, exist_ok=True)
    resolved_clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    lease_stack = ExitStack()
    try:
        runtime_lease = lease_stack.enter_context(_ProductRuntimeLease(root))
    except WorkspaceEconomicLockBusyError as exc:
        raise ProductCompositionError(
            "another Autosport product runtime already owns this workspace"
        ) from exc
    except WorkspaceEconomicLockError as exc:
        raise ProductCompositionError(
            "cannot establish exclusive product runtime workspace authority"
        ) from exc

    with lease_stack:
        settlement_authority_identity = _settlement_authority_identity(
            source=source,
            source_id=source_id,
            outcome_authority=outcome_authority,
        )
        manifest = _ManifestStore(root / "product_composition.json").load_or_create(
            source_id=source_id,
            initial_bankroll=normalized_bankroll,
            settlement_authority_identity=settlement_authority_identity,
        )

        lifecycle = ContinuousEventLifecycle(root / "catalog.json")
        market_store = SQLiteMarketStore(root / "market.db")
        lease_stack.callback(market_store.close)
        mirror = MarketMirror()
        invalidations = BoundedMirrorInvalidationBuffer(mirror)

        for event in market_store.current_by_source().values():
            invalidations.accept_persisted(event)

        market_bus = MarketEventBus(market_store)
        market_bus.subscribe(invalidations.accept_persisted)
        source_health = SourceHealthStore(root / "source_health.json")
        canonical_application = CanonicalDesktopApplication(
            market_bus,
            source_health,
            root / "desktop_application.json",
            clock=resolved_clock,
        )

        dependencies = FocusedMirrorDependencyIndex(mirror)
        collector_store = CollectorDeltaStore(root / "collector_deltas.json")
        collector = HeadlessCollectorService(
            delta_store=collector_store,
            lifecycle=lifecycle,
            source=source,
            state_path=root / "collector_state.json",
            run_id=f"product:{source_id}",
            clock=resolved_clock,
            sleep=sleep,
        )
        desktop = DesktopDeltaConsumer(
            collector_store,
            DesktopDeltaCheckpointStore(root / "desktop_acks.json"),
            resolve_event=source.resolve_event,
            apply_event=canonical_application.apply,
            lookup_application_receipt=canonical_application.lookup_receipt,
        )
        coordinator = ContinuousSessionCoordinator(
            workspace=root,
            collector=collector,
            lifecycle=lifecycle,
            market_store=market_store,
            desktop_consumer=desktop,
            invalidation_buffer=invalidations,
            dependency_index=dependencies,
            outcome_authority=outcome_authority,
            settlement_learning_handoff=settlement_learning_handoff,
            clock=resolved_clock,
            initial_bankroll=manifest.initial_bankroll,
        )
        runtime = AutonomousProductRuntime(
            workspace=root,
            manifest=manifest,
            coordinator=coordinator,
            collector=collector,
            market_store=market_store,
            lifecycle=lifecycle,
            mirror=mirror,
            invalidations=invalidations,
            dependencies=dependencies,
            _runtime_lease=runtime_lease,
            _start_transition_store=_ProductStartTransitionStore(
                root / "product_start_transition.json"
            ),
        )
        runtime._recover_interrupted_start()
        lease_stack.pop_all()
        return runtime