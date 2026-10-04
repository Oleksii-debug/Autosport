from __future__ import annotations

import builtins as _builtins
import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from types import FunctionType, MethodType
from typing import Any, Callable, Protocol

from .causal_collector import (
    CollectorDelta,
    CausalView,
    DesktopDeltaConsumer,
    GapState,
    SyncState,
)
from .collector_service import HeadlessCollectorService
from .event_lifecycle import ContinuousEventLifecycle, EventLifecycleRecord, EventPhase
from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads
from .market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
    MirrorInvalidationBatch,
)
from .paper import PaperBook
from .settlement import SettlementEngine
from .storage import SQLiteMarketStore
from .workspace_lock import WorkspaceEconomicLock


class ContinuousSessionError(RuntimeError):
    """Base error for the durable continuous-session coordinator."""


class SessionPausedError(ContinuousSessionError):
    """Raised when work is attempted while the session is durably PAUSED."""


class SessionStoppedError(ContinuousSessionError):
    """Raised when work is attempted while the session is durably STOPPED."""


def _build_settlement_callback_authority():
    """Build one shared fail-closed witness for settlement callback dispatch."""

    missing_callback_value = object()
    canonical_object_getattribute = object.__getattribute__
    exact_type = type
    exact_type_getattribute = type.__getattribute__
    exact_len = len
    exact_any = any
    exact_zip = zip
    exact_tuple = tuple
    exact_callable = callable
    exact_dict_type = dict
    exact_dict_get = exact_dict_type.get
    canonical_builtins = _builtins.__dict__
    canonical_builtin_bindings = (
        ("type", exact_type),
        ("len", exact_len),
        ("any", exact_any),
        ("zip", exact_zip),
        ("tuple", exact_tuple),
        ("callable", exact_callable),
        ("dict", exact_dict_type),
    )

    def require_canonical_builtins(*, label: str) -> None:
        for name, expected in canonical_builtin_bindings:
            if (
                exact_dict_get(canonical_builtins, name, missing_callback_value)
                is not expected
            ):
                raise ContinuousSessionError(
                    f"settlement {label} builtin authority changed during tick"
                )

    def capture_python_surface(function: FunctionType) -> tuple[object, ...]:
        closure = function.__closure__
        closure_cells = () if closure is None else closure
        try:
            closure_values = exact_tuple(cell.cell_contents for cell in closure_cells)
        except ValueError as exc:
            raise ContinuousSessionError(
                "settlement callback executable closure is unavailable"
            ) from exc
        kwdefaults = function.__kwdefaults__
        kwdefault_items = () if kwdefaults is None else exact_tuple(kwdefaults.items())
        return (
            function,
            function.__code__,
            function.__globals__,
            function.__defaults__,
            kwdefaults,
            kwdefault_items,
            closure,
            exact_tuple(closure_cells),
            closure_values,
        )

    def capture_python_global_graph(
        function: FunctionType,
    ) -> tuple[tuple[object, ...], ...]:
        """Capture the same-module executable globals reachable from one callback."""

        root_globals = function.__globals__
        pending = [function]
        seen: set[FunctionType] = set()
        graph: list[tuple[object, ...]] = []
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            current_builtins = current.__builtins__
            if exact_type(current_builtins) is not dict:
                raise ContinuousSessionError(
                    "settlement callback builtin namespace is unavailable"
                )
            bindings: list[
                tuple[str, object, tuple[object, ...] | None, object]
            ] = []
            for global_name in current.__code__.co_names:
                value = current.__globals__.get(
                    global_name,
                    missing_callback_value,
                )
                value_surface = (
                    capture_python_surface(value)
                    if exact_type(value) is FunctionType
                    else None
                )
                builtin_value = (
                    exact_dict_get(
                        current_builtins,
                        global_name,
                        missing_callback_value,
                    )
                    if value is missing_callback_value
                    else missing_callback_value
                )
                bindings.append(
                    (
                        global_name,
                        value,
                        value_surface,
                        builtin_value,
                    )
                )
                if (
                    value is not missing_callback_value
                    and exact_type(value) is FunctionType
                    and value.__globals__ is root_globals
                    and value not in seen
                ):
                    pending.append(value)
            graph.append(
                (
                    capture_python_surface(current),
                    current_builtins,
                    exact_tuple(bindings),
                )
            )
        return exact_tuple(graph)

    def capture_python_callable(function: FunctionType) -> tuple[object, ...]:
        return (
            *capture_python_surface(function),
            capture_python_global_graph(function),
        )

    def stable_callback_lookup(owner: object, name: str) -> object | None:
        try:
            return canonical_object_getattribute(owner, name)
        except AttributeError:
            return None

    def capture_callback(
        owner: object,
        name: str,
        *,
        optional: bool = False,
    ) -> tuple[object | None, tuple[object, ...] | None]:
        callback = stable_callback_lookup(owner, name)
        if callback is None:
            if optional:
                return None, None
            raise ContinuousSessionError(
                f"settlement {name} dispatch must remain callable"
            )
        if not exact_callable(callback):
            raise ContinuousSessionError(
                f"settlement {name} dispatch must remain callable"
            )
        if exact_type(callback) is MethodType and exact_type(callback.__func__) is FunctionType:
            return callback, (
                "method",
                callback.__self__,
                *capture_python_callable(callback.__func__),
            )
        if exact_type(callback) is FunctionType:
            return callback, ("function", *capture_python_callable(callback))
        callback_type = exact_type(callback)
        call_target = exact_type_getattribute(callback_type, "__call__")
        call_witness = (
            capture_python_callable(call_target)
            if exact_type(call_target) is FunctionType
            else None
        )
        return callback, (
            "opaque",
            callback,
            callback_type,
            call_target,
            call_witness,
        )

    def require_python_surface(
        witness: tuple[object, ...],
        *,
        label: str,
    ) -> None:
        (
            function,
            expected_code,
            expected_globals,
            expected_defaults,
            expected_kwdefaults,
            expected_kwdefault_items,
            expected_closure,
            expected_closure_cells,
            expected_closure_values,
        ) = witness
        if (
            exact_type(function) is not FunctionType
            or function.__code__ is not expected_code
            or function.__globals__ is not expected_globals
            or function.__defaults__ is not expected_defaults
            or function.__kwdefaults__ is not expected_kwdefaults
            or function.__closure__ is not expected_closure
        ):
            raise ContinuousSessionError(
                f"settlement {label} executable changed during tick"
            )
        if expected_kwdefaults is not None:
            if (
                exact_len(expected_kwdefaults) != exact_len(expected_kwdefault_items)
                or exact_any(
                    expected_kwdefaults.get(key, missing_callback_value) is not value
                    for key, value in expected_kwdefault_items
                )
            ):
                raise ContinuousSessionError(
                    f"settlement {label} executable changed during tick"
                )
        current_closure = function.__closure__
        current_cells = () if current_closure is None else current_closure
        if (
            exact_len(current_cells) != exact_len(expected_closure_cells)
            or exact_any(
                current is not expected
                for current, expected in exact_zip(
                    current_cells,
                    expected_closure_cells,
                )
            )
        ):
            raise ContinuousSessionError(
                f"settlement {label} executable changed during tick"
            )
        try:
            current_values = exact_tuple(cell.cell_contents for cell in current_cells)
        except ValueError as exc:
            raise ContinuousSessionError(
                f"settlement {label} executable changed during tick"
            ) from exc
        if (
            exact_len(current_values) != exact_len(expected_closure_values)
            or exact_any(
                current is not expected
                for current, expected in exact_zip(
                    current_values,
                    expected_closure_values,
                )
            )
        ):
            raise ContinuousSessionError(
                f"settlement {label} executable changed during tick"
            )

    def require_python_global_graph(
        graph: tuple[tuple[object, ...], ...],
        *,
        label: str,
    ) -> None:
        for function_witness, expected_builtins, bindings in graph:
            require_python_surface(function_witness, label=label)
            function = function_witness[0]
            if (
                exact_type(function) is not FunctionType
                or function.__builtins__ is not expected_builtins
                or exact_type(expected_builtins) is not dict
            ):
                raise ContinuousSessionError(
                    f"settlement {label} builtin namespace changed during tick"
                )
            expected_globals = function_witness[2]
            for (
                global_name,
                expected,
                nested_surface,
                expected_builtin,
            ) in bindings:
                if expected is missing_callback_value:
                    if global_name in expected_globals:
                        raise ContinuousSessionError(
                            f"settlement {label} global binding changed during tick"
                        )
                    if (
                        exact_dict_get(
                            expected_builtins,
                            global_name,
                            missing_callback_value,
                        )
                        is not expected_builtin
                    ):
                        raise ContinuousSessionError(
                            f"settlement {label} builtin binding changed during tick"
                        )
                    continue
                if (
                    global_name not in expected_globals
                    or expected_globals[global_name] is not expected
                ):
                    raise ContinuousSessionError(
                        f"settlement {label} global binding changed during tick"
                    )
                if nested_surface is not None:
                    require_python_surface(
                        nested_surface,
                        label=f"{label} global {global_name}",
                    )

    def require_python_callable(
        witness: tuple[object, ...],
        *,
        label: str,
    ) -> None:
        if exact_len(witness) != 10:
            raise ContinuousSessionError(
                f"settlement {label} dispatch witness is unavailable"
            )
        surface = witness[:9]
        global_graph = witness[9]
        require_python_surface(surface, label=label)
        require_python_global_graph(global_graph, label=label)

    def require_callback(
        owner: object,
        name: str,
        callback: object | None,
        witness: tuple[object, ...] | None,
        *,
        label: str,
        optional: bool = False,
        relookup: bool = True,
    ) -> None:
        require_canonical_builtins(label=label)
        current = stable_callback_lookup(owner, name) if relookup else callback
        if callback is None:
            if not optional or current is not None:
                raise ContinuousSessionError(
                    f"settlement {label} dispatch changed during tick"
                )
            return
        if witness is None:
            raise ContinuousSessionError(
                f"settlement {label} dispatch witness is unavailable"
            )
        kind = witness[0]
        if kind == "method":
            expected_self = witness[1]
            function_witness = witness[2:]
            expected_function = function_witness[0]
            if (
                exact_type(current) is not MethodType
                or current.__self__ is not expected_self
                or current.__func__ is not expected_function
            ):
                raise ContinuousSessionError(
                    f"settlement {label} dispatch changed during tick"
                )
            require_python_callable(function_witness, label=label)
            return
        if kind == "function":
            function_witness = witness[1:]
            expected_function = function_witness[0]
            if current is not expected_function:
                raise ContinuousSessionError(
                    f"settlement {label} dispatch changed during tick"
                )
            require_python_callable(function_witness, label=label)
            return
        if kind != "opaque" or current is not witness[1] or exact_type(current) is not witness[2]:
            raise ContinuousSessionError(
                f"settlement {label} dispatch changed during tick"
            )
        expected_type = witness[2]
        expected_call_target = witness[3]
        call_witness = witness[4]
        if exact_type_getattribute(expected_type, "__call__") is not expected_call_target:
            raise ContinuousSessionError(
                f"settlement {label} invocation slot changed during tick"
            )
        if call_witness is not None:
            require_python_callable(call_witness, label=f"{label} invocation")


    return capture_callback, require_callback


(
    _CANONICAL_CAPTURE_SETTLEMENT_CALLBACK,
    _CANONICAL_REQUIRE_SETTLEMENT_CALLBACK,
) = _build_settlement_callback_authority()
del _build_settlement_callback_authority


def _bind_canonical_settlement_engine(method):
    """Bind the exact engine and workspace lock used by the sealed consumer."""

    canonical_engine_type = SettlementEngine
    canonical_lock_type = WorkspaceEconomicLock
    canonical_snapshot = _canonical_settlement_handoff_snapshot

    def guarded(self, *args, **kwargs):
        if "_settlement_engine_type" in kwargs:
            raise TypeError("settlement engine origin is internal product authority")
        if "_economic_lock_type" in kwargs or "_snapshot_fn" in kwargs:
            raise TypeError("settlement economic origin is internal product authority")
        kwargs["_settlement_engine_type"] = canonical_engine_type
        kwargs["_economic_lock_type"] = canonical_lock_type
        kwargs["_snapshot_fn"] = canonical_snapshot
        return method(self, *args, **kwargs)

    guarded.__name__ = method.__name__
    guarded.__qualname__ = method.__qualname__
    guarded.__doc__ = method.__doc__
    guarded.__annotations__ = method.__annotations__
    return guarded


def _bind_canonical_settlement_snapshot(method):
    """Bind settlement snapshot and callback lookup to import-time authorities."""

    canonical_snapshot = _canonical_settlement_handoff_snapshot
    canonical_snapshot_code = canonical_snapshot.__code__
    canonical_object_getattribute = object.__getattribute__
    canonical_capture_callback = _CANONICAL_CAPTURE_SETTLEMENT_CALLBACK
    canonical_require_callback = _CANONICAL_REQUIRE_SETTLEMENT_CALLBACK

    def guarded(self, *args, **kwargs):
        if (
            "_snapshot_fn" in kwargs
            or "_object_getattribute" in kwargs
            or "_capture_callback_fn" in kwargs
            or "_require_callback_fn" in kwargs
        ):
            raise TypeError("settlement internal dispatch origin is product authority")
        if canonical_snapshot.__code__ is not canonical_snapshot_code:
            raise ContinuousSessionError("settlement snapshot executable changed")
        exposed_snapshot = getattr(
            self,
            "_settlement_handoff_snapshot",
            canonical_snapshot,
        )
        if exposed_snapshot is not canonical_snapshot:
            raise ContinuousSessionError("settlement snapshot dispatch changed")
        kwargs["_snapshot_fn"] = canonical_snapshot
        kwargs["_object_getattribute"] = canonical_object_getattribute
        kwargs["_capture_callback_fn"] = canonical_capture_callback
        kwargs["_require_callback_fn"] = canonical_require_callback
        result = method(self, *args, **kwargs)
        if canonical_snapshot.__code__ is not canonical_snapshot_code:
            raise ContinuousSessionError("settlement snapshot executable changed")
        return result

    guarded.__name__ = method.__name__
    guarded.__qualname__ = method.__qualname__
    guarded.__doc__ = method.__doc__
    guarded.__annotations__ = method.__annotations__
    return guarded


def _seal_settlement_consumer_entry(method):
    """Return an immutable built-in descriptor with a closure-owned dispatch target."""

    def resolve(instance):
        return method.__get__(instance, type(instance))

    def reject_set(_instance, _value) -> None:
        raise TypeError("canonical settlement consumer entry binding is immutable")

    def reject_delete(_instance) -> None:
        raise TypeError("canonical settlement consumer entry binding is immutable")

    return property(resolve, reject_set, reject_delete, method.__doc__)


def _build_settlement_consumer_class_guard(name: str):
    """Keep type-level replacement from bypassing the installed data descriptor."""

    class SettlementConsumerClassGuard:
        __slots__ = ()

        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            binding = instance.__dict__[name]
            return binding.__get__(None, instance)

        def __set__(self, _instance, _value) -> None:
            raise TypeError("canonical settlement consumer entry binding is immutable")

        def __delete__(self, _instance) -> None:
            raise TypeError("canonical settlement consumer entry binding is immutable")

    return SettlementConsumerClassGuard()


class _ContinuousSessionCoordinatorMeta(type):
    """Seal the trusted settlement consumer entry inside the process TCB."""

    def __init_subclass__(mcls, **kwargs) -> None:
        raise TypeError("canonical settlement consumer metaclass is not extensible")

    def __new__(mcls, name, bases, namespace, **kwargs):
        protected = {"_settle", "_settlement_consumer_bindings_sealed"}
        inherits_sealed_consumer = any(
            any(
                ancestor.__dict__.get(
                    "_settlement_consumer_bindings_sealed",
                    False,
                )
                for ancestor in base.__mro__
            )
            for base in bases
        )
        if inherits_sealed_consumer and protected.intersection(namespace):
            raise TypeError("canonical settlement consumer entry binding is immutable")
        return super().__new__(mcls, name, bases, namespace, **kwargs)

    def __setattr__(cls, name: str, value: object) -> None:
        sealed = any(
            ancestor.__dict__.get("_settlement_consumer_bindings_sealed", False)
            for ancestor in cls.__mro__
        )
        if sealed and name in {
            "_settle",
            "_settlement_consumer_bindings_sealed",
        }:
            raise TypeError("canonical settlement consumer entry binding is immutable")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        sealed = any(
            ancestor.__dict__.get("_settlement_consumer_bindings_sealed", False)
            for ancestor in cls.__mro__
        )
        if sealed and name in {
            "_settle",
            "_settlement_consumer_bindings_sealed",
        }:
            raise TypeError("canonical settlement consumer entry binding is immutable")
        super().__delattr__(name)


class SessionState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class SettlementResolution:
    """One externally-authoritative, causally available settlement resolution."""

    event_identity: str
    settlement_ref: str
    quote_outcomes: dict[str, str]
    evidence_id: str
    evidence_sha256: str
    available_at: str

    def validate(self, *, as_of: str) -> None:
        _text(self.event_identity, "event_identity")
        _text(self.settlement_ref, "settlement_ref")
        _text(self.evidence_id, "evidence_id")
        _sha256(self.evidence_sha256, "evidence_sha256")
        cutoff = _instant(as_of, "as_of")
        available = _instant(self.available_at, "available_at")
        if available > cutoff:
            raise ValueError("settlement evidence is not causally available at session cutoff")
        if type(self.quote_outcomes) not in {dict, _ValidatedQuoteOutcomes} or not self.quote_outcomes:
            raise ValueError("quote_outcomes must be a non-empty exact dict")
        for quote_key, outcome in self.quote_outcomes.items():
            _text(quote_key, "quote_outcomes quote_key")
            if type(outcome) is not str or outcome not in {"win", "loss", "void"}:
                raise ValueError("quote_outcomes contains unsupported outcome")

        quote_outcomes_sha256 = _settlement_quote_outcomes_sha256(self.quote_outcomes)
        if type(self.quote_outcomes) is _ValidatedQuoteOutcomes:
            if self.quote_outcomes.validated_sha256 != quote_outcomes_sha256:
                raise ValueError(
                    "settlement resolution quote_outcomes changed after validation"
                )
        else:
            object.__setattr__(
                self,
                "quote_outcomes",
                _ValidatedQuoteOutcomes(
                    self.quote_outcomes,
                    validated_sha256=quote_outcomes_sha256,
                ),
            )


class SettlementOutcomeAuthority(Protocol):
    """External outcome authority; Autosport never derives outcomes from lifecycle state."""

    def resolve(
        self,
        record: EventLifecycleRecord,
        *,
        as_of: str,
    ) -> SettlementResolution | None:
        ...


class SettlementLearningHandoff(Protocol):
    """Optional durable PAPER settlement seam for causal learning."""

    def prepare_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        at: str,
    ) -> tuple[str, ...]:
        ...

    def reconcile_after_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        settled_ticket_ids: tuple[str, ...],
        at: str,
    ) -> tuple[str, ...]:
        ...


@dataclass(frozen=True, slots=True)
class ContinuousTickResult:
    session_id: str
    cycle_index: int
    source_id: str
    source_provider_unavailable: bool
    source_gap_states: tuple[str, ...]
    source_sync_states: tuple[str, ...]
    committed_delta_ids: tuple[str, ...]
    delivered_delta_ids: tuple[str, ...]
    affected_input_ids: tuple[str, ...]
    registered_input_ids: tuple[str, ...]
    retired_input_ids: tuple[str, ...]
    full_refresh_required: bool
    invalidation_backlog: bool
    settled_ticket_ids: tuple[str, ...]
    settlement_evidence_ids: tuple[str, ...]
    last_success_at: str | None


@dataclass(frozen=True, slots=True)
class ContinuousSessionStatus:
    session_id: str
    source_id: str
    state: SessionState
    cycles_completed: int
    last_success_at: str | None
    last_error_code: str | None
    last_full_refresh_at: str | None
    settlement_evidence: tuple[dict[str, str | None], ...]
    source_provider_unavailable: bool = False
    source_last_success_at: str | None = None
    source_last_error_code: str | None = None
    source_gap_state: str | None = None
    source_sync_state: str | None = None
    source_state_delta_id: str | None = None
    source_unresolved_gap_delta_ids: tuple[str, ...] = ()
    source_projection_stream_epoch: str | None = None
    source_state_projection_backlog: bool = False
    invalidation_pending_count: int = 0
    invalidation_full_refresh_required: bool = False
    settlement_evidence_count: int = 0
    settlement_evidence_materialized: bool = True


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field} must be a non-empty trimmed string")
    return value


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware ISO-8601")
    return parsed.astimezone(timezone.utc)


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


class _ValidatedQuoteOutcomes(dict[str, str]):
    """Mutable compatibility view carrying the exact content sealed at validation."""

    __slots__ = ("_validated_sha256",)

    def __init__(
        self,
        values: Any = (),
        *,
        validated_sha256: str | None = None,
    ) -> None:
        super().__init__(values)
        self._validated_sha256 = (
            _settlement_quote_outcomes_sha256(self)
            if validated_sha256 is None
            else _sha256(validated_sha256, "validated_sha256")
        )

    @property
    def validated_sha256(self) -> str:
        return self._validated_sha256


def _settlement_quote_outcomes_sha256(outcomes: dict[str, str]) -> str:
    if type(outcomes) not in {dict, _ValidatedQuoteOutcomes} or not outcomes:
        raise ValueError("quote_outcomes must be a non-empty exact dict")
    canonical: list[list[str]] = []
    for quote_key in sorted(outcomes):
        _text(quote_key, "quote_outcomes quote_key")
        outcome = outcomes[quote_key]
        if type(outcome) is not str or outcome not in {"win", "loss", "void"}:
            raise ValueError("quote_outcomes contains unsupported outcome")
        canonical.append([quote_key, outcome])
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_settlement_handoff_snapshot(
    resolutions: tuple[SettlementResolution, ...],
    *,
    as_of: str | None = None,
) -> tuple[SettlementResolution, ...]:
    """Detach one exact validated settlement snapshot from caller-owned state."""

    detached: list[SettlementResolution] = []
    for resolution in resolutions:
        if type(resolution) is not SettlementResolution:
            raise TypeError(
                "resolutions must contain exact SettlementResolution values"
            )
        validation_cutoff = resolution.available_at if as_of is None else as_of
        resolution.validate(as_of=validation_cutoff)
        outcomes = resolution.quote_outcomes
        if type(outcomes) is not _ValidatedQuoteOutcomes:
            raise ContinuousSessionError(
                "validated settlement outcomes lost canonical seal"
            )
        validated_sha256 = outcomes.validated_sha256
        copied_outcomes = dict(outcomes)
        if (
            _settlement_quote_outcomes_sha256(copied_outcomes)
            != validated_sha256
        ):
            raise ContinuousSessionError(
                "settlement outcomes changed while creating canonical snapshot"
            )
        snapshot = SettlementResolution(
            event_identity=resolution.event_identity,
            settlement_ref=resolution.settlement_ref,
            quote_outcomes=copied_outcomes,
            evidence_id=resolution.evidence_id,
            evidence_sha256=resolution.evidence_sha256,
            available_at=resolution.available_at,
        )
        snapshot.validate(as_of=validation_cutoff)
        snapshot_outcomes = snapshot.quote_outcomes
        if (
            type(snapshot_outcomes) is not _ValidatedQuoteOutcomes
            or snapshot_outcomes.validated_sha256 != validated_sha256
        ):
            raise ContinuousSessionError(
                "canonical settlement snapshot digest mismatch"
            )
        detached.append(snapshot)
    return tuple(detached)


class _ContinuousSessionState:
    _SCHEMA = "autosport.continuous_session"
    _V2_VERSION = 2
    _V3_VERSION = 3
    # Wave M / PR #2013 owns embedded schema v3 for settlement outcome
    # fingerprints. The bounded journal is v4 and migrates both embedded
    # predecessors without dropping v3 fingerprint authority.
    _VERSION = 4
    _EVIDENCE_SCHEMA = "autosport.continuous_session.settlement_evidence"
    _LEGACY_EVIDENCE_VERSION = 1
    _EVIDENCE_VERSION = 2
    _EMPTY_EVIDENCE_TIP = "0" * 64
    _STATE_FIELDS = {
        "session_id",
        "source_id",
        "state",
        "started_at",
        "cycles_completed",
        "last_success_at",
        "last_error_code",
        "last_full_refresh_at",
        "source_gap_state",
        "source_sync_state",
        "source_state_delta_id",
        "source_unresolved_gap_delta_ids",
        "source_projection_stream_epoch",
        "source_state_projection_backlog",
    }
    _V2_FIELDS = {
        "schema",
        "schema_version",
        *_STATE_FIELDS,
        "settlement_evidence",
    }
    _FIELDS = {
        "schema",
        "schema_version",
        *_STATE_FIELDS,
        "settlement_evidence_count",
        "settlement_evidence_tip_sha256",
        "settlement_evidence_tip_key_sha256",
        "settlement_evidence_pending",
    }
    _LEGACY_EVIDENCE_FIELDS = {
        "schema",
        "schema_version",
        "sequence",
        "previous_record_sha256",
        "evidence_key_sha256",
        "event_identity",
        "settlement_ref",
        "evidence_id",
        "evidence_sha256",
        "available_at",
        "record_sha256",
    }
    _EVIDENCE_FIELDS = _LEGACY_EVIDENCE_FIELDS | {"quote_outcomes_sha256"}
    _PENDING_FIELDS = {
        "base_count",
        "base_tip_sha256",
        "success_at",
        "full_refresh",
        "records",
    }

    def __init__(
        self,
        path: str | Path,
        *,
        session_id: str | None,
        source_id: str,
        clock: Callable[[], str],
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_dir = self.path.with_name(
            f"{self.path.stem}.settlement-evidence"
        )
        self.source_id = _text(source_id, "source_id")
        self._clock = clock

        with durable_path_lock(self.path):
            if self.path.exists():
                raw = self._read_file()
                if session_id is not None and raw["session_id"] != _text(
                    session_id, "session_id"
                ):
                    raise ContinuousSessionError(
                        "durable session_id does not match configured session"
                    )
                if raw["schema_version"] in {
                    self._V2_VERSION,
                    self._V3_VERSION,
                }:
                    raw = self._migrate_embedded_locked(raw)
                else:
                    raw = self._recover_pending_locked(raw)
                    self._load_evidence_history(raw)
                existing_source = raw["source_id"]
                if existing_source != self.source_id:
                    raise ContinuousSessionError(
                        "durable session source_id does not match configured source"
                    )
            else:
                if self.evidence_dir.is_symlink() or self.evidence_dir.exists():
                    raise ContinuousSessionError(
                        "new continuous session conflicts with orphan settlement evidence journal"
                    )
                resolved_id = _text(
                    session_id or str(uuid.uuid4()),
                    "session_id",
                )
                started_at = clock()
                _instant(started_at, "started_at")
                raw = {
                    "schema": self._SCHEMA,
                    "schema_version": self._VERSION,
                    "session_id": resolved_id,
                    "source_id": self.source_id,
                    "state": SessionState.RUNNING.value,
                    "started_at": started_at,
                    "cycles_completed": 0,
                    "last_success_at": None,
                    "last_error_code": None,
                    "last_full_refresh_at": None,
                    "source_gap_state": None,
                    "source_sync_state": None,
                    "source_state_delta_id": None,
                    "source_unresolved_gap_delta_ids": [],
                    "source_projection_stream_epoch": None,
                    "source_state_projection_backlog": False,
                    "settlement_evidence_count": 0,
                    "settlement_evidence_tip_sha256": self._EMPTY_EVIDENCE_TIP,
                    "settlement_evidence_tip_key_sha256": self._EMPTY_EVIDENCE_TIP,
                    "settlement_evidence_pending": None,
                }
                self._write_state_locked(raw)
                self._load_evidence_history(raw)

    @classmethod
    def _validate_settlement_evidence(
        cls,
        raw: object,
        *,
        schema_version: int,
    ) -> tuple[dict[str, str | None], ...]:
        if type(raw) is not list:
            raise ContinuousSessionError("settlement_evidence must be a list")
        values: list[dict[str, str | None]] = []
        seen_ids: set[str] = set()
        seen_event_refs: set[tuple[str, str]] = set()
        legacy_fields = {
            "event_identity",
            "settlement_ref",
            "evidence_id",
            "evidence_sha256",
            "available_at",
        }
        current_fields = legacy_fields | {"quote_outcomes_sha256"}
        if schema_version == cls._V2_VERSION:
            expected_fields = legacy_fields
        elif schema_version == cls._V3_VERSION:
            expected_fields = current_fields
        else:
            raise ContinuousSessionError(
                "unsupported embedded settlement evidence schema"
            )
        for item in raw:
            if type(item) is not dict or set(item) != expected_fields:
                raise ContinuousSessionError(
                    "settlement_evidence entry fields mismatch"
                )
            event_identity = _text(
                item["event_identity"],
                "settlement_evidence event_identity",
            )
            settlement_ref = _text(
                item["settlement_ref"],
                "settlement_evidence settlement_ref",
            )
            evidence_id = _text(
                item["evidence_id"],
                "settlement_evidence evidence_id",
            )
            if evidence_id in seen_ids:
                raise ContinuousSessionError(
                    "settlement_evidence contains duplicate evidence_id"
                )
            seen_ids.add(evidence_id)
            event_ref = (event_identity, settlement_ref)
            if event_ref in seen_event_refs:
                raise ContinuousSessionError(
                    "settlement_evidence contains duplicate event/reference authority"
                )
            seen_event_refs.add(event_ref)
            evidence_sha256 = _sha256(
                item["evidence_sha256"],
                "settlement_evidence evidence_sha256",
            )
            available_at = _instant(
                item["available_at"],
                "settlement_evidence available_at",
            ).isoformat()
            quote_outcomes_sha256 = (
                None
                if schema_version == cls._V2_VERSION
                else _sha256(
                    item["quote_outcomes_sha256"],
                    "settlement_evidence quote_outcomes_sha256",
                )
            )
            values.append(
                {
                    "event_identity": event_identity,
                    "settlement_ref": settlement_ref,
                    "evidence_id": evidence_id,
                    "evidence_sha256": evidence_sha256,
                    "available_at": available_at,
                    "quote_outcomes_sha256": quote_outcomes_sha256,
                }
            )
        return tuple(values)

    def _validate_common_state(self, raw: dict[str, Any]) -> None:
        if raw["source_id"] != self.source_id:
            raise ContinuousSessionError(
                "continuous session state schema/identity mismatch"
            )
        _text(raw["session_id"], "session_id")
        _instant(raw["started_at"], "started_at")
        try:
            state = SessionState(raw["state"])
        except ValueError as exc:
            raise ContinuousSessionError(
                "unsupported continuous session state"
            ) from exc
        cycles = raw["cycles_completed"]
        if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
            raise ContinuousSessionError(
                "cycles_completed must be a non-negative integer"
            )
        for name in ("last_success_at", "last_full_refresh_at"):
            if raw[name] is not None:
                _instant(raw[name], name)
        if raw["last_error_code"] is not None:
            _text(raw["last_error_code"], "last_error_code")

        gap_state = raw["source_gap_state"]
        sync_state = raw["source_sync_state"]
        if (gap_state is None) != (sync_state is None):
            raise ContinuousSessionError(
                "source gap/sync projection must be present or absent together"
            )
        if gap_state is not None:
            try:
                GapState(gap_state)
                SyncState(sync_state)
            except ValueError as exc:
                raise ContinuousSessionError(
                    "source gap/sync projection contains an unsupported state"
                ) from exc
        if raw["source_state_delta_id"] is not None:
            _text(raw["source_state_delta_id"], "source_state_delta_id")
        if raw["source_projection_stream_epoch"] is not None:
            _text(
                raw["source_projection_stream_epoch"],
                "source_projection_stream_epoch",
            )
        if (raw["source_state_delta_id"] is None) != (
            raw["source_projection_stream_epoch"] is None
        ):
            raise ContinuousSessionError(
                "source projection identity is incomplete"
            )
        if raw["source_state_delta_id"] is None and gap_state is not None:
            raise ContinuousSessionError(
                "source projection state requires a canonical delta identity"
            )
        unresolved = raw["source_unresolved_gap_delta_ids"]
        if (
            type(unresolved) is not list
            or any(type(item) is not str or not item.strip() for item in unresolved)
            or len(set(unresolved)) != len(unresolved)
        ):
            raise ContinuousSessionError(
                "source_unresolved_gap_delta_ids must contain unique non-empty strings"
            )
        if type(raw["source_state_projection_backlog"]) is not bool:
            raise ContinuousSessionError(
                "source_state_projection_backlog must be boolean"
            )
        if unresolved and (
            gap_state != GapState.DETECTED.value
            or sync_state != SyncState.GAP_DETECTED.value
        ):
            raise ContinuousSessionError(
                "unresolved source gaps require DETECTED/GAP_DETECTED projection"
            )
        raw["state"] = state.value

    def _validate_v2(self, raw: dict[str, Any]) -> dict[str, Any]:
        if (
            set(raw) != self._V2_FIELDS
            or raw["schema"] != self._SCHEMA
            or raw["schema_version"] != self._V2_VERSION
        ):
            raise ContinuousSessionError(
                "continuous session state schema/identity mismatch"
            )
        self._validate_common_state(raw)
        raw["settlement_evidence"] = [
            dict(item)
            for item in self._validate_settlement_evidence(
                raw["settlement_evidence"],
                schema_version=self._V2_VERSION,
            )
        ]
        return raw

    def _validate_v3(self, raw: dict[str, Any]) -> dict[str, Any]:
        if (
            set(raw) != self._V2_FIELDS
            or raw["schema"] != self._SCHEMA
            or raw["schema_version"] != self._V3_VERSION
        ):
            raise ContinuousSessionError(
                "continuous session state schema/identity mismatch"
            )
        self._validate_common_state(raw)
        raw["settlement_evidence"] = [
            dict(item)
            for item in self._validate_settlement_evidence(
                raw["settlement_evidence"],
                schema_version=self._V3_VERSION,
            )
        ]
        return raw

    @staticmethod
    def _canonical_sha256(payload: dict[str, Any]) -> str:
        try:
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ContinuousSessionError(
                "continuous session evidence is outside canonical JSON domain"
            ) from exc
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _evidence_key(evidence_id: str) -> str:
        return hashlib.sha256(evidence_id.encode("utf-8")).hexdigest()

    def _validate_evidence_record(self, raw: object) -> dict[str, Any]:
        if type(raw) is not dict or raw.get("schema") != self._EVIDENCE_SCHEMA:
            raise ContinuousSessionError(
                "settlement evidence journal record schema mismatch"
            )
        record_version = raw.get("schema_version")
        if record_version == self._LEGACY_EVIDENCE_VERSION:
            expected_fields = self._LEGACY_EVIDENCE_FIELDS
        elif record_version == self._EVIDENCE_VERSION:
            expected_fields = self._EVIDENCE_FIELDS
        else:
            raise ContinuousSessionError(
                "settlement evidence journal record schema mismatch"
            )
        if set(raw) != expected_fields:
            raise ContinuousSessionError(
                "settlement evidence journal record schema mismatch"
            )
        sequence = raw["sequence"]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= 0
        ):
            raise ContinuousSessionError(
                "settlement evidence sequence must be a positive integer"
            )
        _sha256(
            raw["previous_record_sha256"],
            "settlement evidence previous_record_sha256",
        )
        evidence_id = _text(
            raw["evidence_id"],
            "settlement evidence evidence_id",
        )
        expected_key = self._evidence_key(evidence_id)
        if (
            _sha256(
                raw["evidence_key_sha256"],
                "settlement evidence evidence_key_sha256",
            )
            != expected_key
        ):
            raise ContinuousSessionError(
                "settlement evidence key digest mismatch"
            )
        _text(raw["event_identity"], "settlement evidence event_identity")
        _text(raw["settlement_ref"], "settlement evidence settlement_ref")
        _sha256(
            raw["evidence_sha256"],
            "settlement evidence evidence_sha256",
        )
        _instant(raw["available_at"], "settlement evidence available_at")
        if record_version == self._EVIDENCE_VERSION:
            _sha256(
                raw["quote_outcomes_sha256"],
                "settlement evidence quote_outcomes_sha256",
            )
        record_sha256 = _sha256(
            raw["record_sha256"],
            "settlement evidence record_sha256",
        )
        payload = dict(raw)
        del payload["record_sha256"]
        if record_sha256 != self._canonical_sha256(payload):
            raise ContinuousSessionError(
                "settlement evidence journal record digest mismatch"
            )
        return raw

    def _validate_pending(self, raw: object, state: dict[str, Any]) -> dict[str, Any]:
        if type(raw) is not dict or set(raw) != self._PENDING_FIELDS:
            raise ContinuousSessionError(
                "settlement evidence pending transaction schema mismatch"
            )
        base_count = raw["base_count"]
        if (
            isinstance(base_count, bool)
            or not isinstance(base_count, int)
            or base_count < 0
            or base_count != state["settlement_evidence_count"]
        ):
            raise ContinuousSessionError(
                "settlement evidence pending base count mismatch"
            )
        base_tip = _sha256(
            raw["base_tip_sha256"],
            "settlement evidence pending base_tip_sha256",
        )
        if base_tip != state["settlement_evidence_tip_sha256"]:
            raise ContinuousSessionError(
                "settlement evidence pending base tip mismatch"
            )
        _instant(raw["success_at"], "settlement evidence pending success_at")
        if type(raw["full_refresh"]) is not bool:
            raise ContinuousSessionError(
                "settlement evidence pending full_refresh must be boolean"
            )
        records = raw["records"]
        if type(records) is not list or not records:
            raise ContinuousSessionError(
                "settlement evidence pending records must be non-empty"
            )
        previous = base_tip
        expected_sequence = base_count + 1
        seen_keys: set[str] = set()
        seen_event_refs: set[tuple[str, str]] = set()
        for item in records:
            record = self._validate_evidence_record(item)
            if record["sequence"] != expected_sequence:
                raise ContinuousSessionError(
                    "settlement evidence pending sequence is not contiguous"
                )
            if record["previous_record_sha256"] != previous:
                raise ContinuousSessionError(
                    "settlement evidence pending chain mismatch"
                )
            key = record["evidence_key_sha256"]
            if key in seen_keys:
                raise ContinuousSessionError(
                    "settlement evidence pending contains duplicate key"
                )
            seen_keys.add(key)
            event_ref = (record["event_identity"], record["settlement_ref"])
            if event_ref in seen_event_refs:
                raise ContinuousSessionError(
                    "settlement evidence pending contains duplicate event/reference authority"
                )
            seen_event_refs.add(event_ref)
            previous = record["record_sha256"]
            expected_sequence += 1
        return raw

    def _validate_v4(self, raw: dict[str, Any]) -> dict[str, Any]:
        if (
            set(raw) != self._FIELDS
            or raw["schema"] != self._SCHEMA
            or raw["schema_version"] != self._VERSION
        ):
            raise ContinuousSessionError(
                "continuous session state schema/identity mismatch"
            )
        self._validate_common_state(raw)
        count = raw["settlement_evidence_count"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            raise ContinuousSessionError(
                "settlement_evidence_count must be a non-negative integer"
            )
        tip = _sha256(
            raw["settlement_evidence_tip_sha256"],
            "settlement_evidence_tip_sha256",
        )
        tip_key = _sha256(
            raw["settlement_evidence_tip_key_sha256"],
            "settlement_evidence_tip_key_sha256",
        )
        if count == 0:
            if (
                tip != self._EMPTY_EVIDENCE_TIP
                or tip_key != self._EMPTY_EVIDENCE_TIP
            ):
                raise ContinuousSessionError(
                    "empty settlement evidence history has non-empty tip"
                )
        elif (
            tip == self._EMPTY_EVIDENCE_TIP
            or tip_key == self._EMPTY_EVIDENCE_TIP
        ):
            raise ContinuousSessionError(
                "non-empty settlement evidence history lacks a tip"
            )
        pending = raw["settlement_evidence_pending"]
        if pending is not None:
            self._validate_pending(pending, raw)
        return raw

    def _read_file(self) -> dict[str, Any]:
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ContinuousSessionError(
                "cannot verify continuous session state"
            ) from exc
        if type(raw) is not dict:
            raise ContinuousSessionError(
                "continuous session state schema/identity mismatch"
            )
        version = raw.get("schema_version")
        if version == self._V2_VERSION:
            return self._validate_v2(raw)
        if version == self._V3_VERSION:
            return self._validate_v3(raw)
        if version == self._VERSION:
            return self._validate_v4(raw)
        raise ContinuousSessionError(
            "unsupported continuous session state schema version"
        )

    def _ensure_evidence_dir(self) -> None:
        if self.evidence_dir.exists():
            if self.evidence_dir.is_symlink() or not self.evidence_dir.is_dir():
                raise ContinuousSessionError(
                    "settlement evidence journal path is not a canonical directory"
                )
            return
        try:
            self.evidence_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            if self.evidence_dir.is_symlink() or not self.evidence_dir.is_dir():
                raise ContinuousSessionError(
                    "settlement evidence journal path is not a canonical directory"
                )
        except OSError as exc:
            raise ContinuousSessionError(
                "cannot create settlement evidence journal"
            ) from exc

    def _evidence_path(self, evidence_key_sha256: str) -> Path:
        key = _sha256(
            evidence_key_sha256,
            "settlement evidence key path",
        )
        return self.evidence_dir / f"{key}.json"

    def _read_evidence_path(self, path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ContinuousSessionError(
                "settlement evidence journal entry is not a regular file"
            )
        try:
            raw = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ContinuousSessionError(
                "cannot verify settlement evidence journal entry"
            ) from exc
        record = self._validate_evidence_record(raw)
        if path.name != f"{record['evidence_key_sha256']}.json":
            raise ContinuousSessionError(
                "settlement evidence journal filename/key mismatch"
            )
        return record

    def _record_paths(self) -> tuple[Path, ...]:
        if not self.evidence_dir.exists():
            return ()
        if self.evidence_dir.is_symlink() or not self.evidence_dir.is_dir():
            raise ContinuousSessionError(
                "settlement evidence journal path is not a canonical directory"
            )
        try:
            entries = tuple(self.evidence_dir.iterdir())
        except OSError as exc:
            raise ContinuousSessionError(
                "cannot enumerate settlement evidence journal"
            ) from exc
        records: list[Path] = []
        for path in entries:
            if path.name.startswith("."):
                continue
            if path.suffix != ".json":
                raise ContinuousSessionError(
                    "settlement evidence journal contains an unexpected entry"
                )
            if path.is_symlink() or not path.is_file():
                raise ContinuousSessionError(
                    "settlement evidence journal contains a non-regular entry"
                )
            records.append(path)
        return tuple(records)

    def _write_evidence_record(self, record: dict[str, Any]) -> None:
        record = self._validate_evidence_record(dict(record))
        self._ensure_evidence_dir()
        path = self._evidence_path(record["evidence_key_sha256"])
        if path.exists():
            if self._read_evidence_path(path) != record:
                raise ContinuousSessionError(
                    "settlement evidence id conflicts with durable evidence"
                )
            return
        atomic_write_json(path, record)
        if self._read_evidence_path(path) != record:
            raise ContinuousSessionError(
                "settlement evidence journal publication mismatch"
            )

    @staticmethod
    def _normalized_item_from_record(
        record: dict[str, Any],
    ) -> dict[str, str | None]:
        return {
            "event_identity": record["event_identity"],
            "settlement_ref": record["settlement_ref"],
            "evidence_id": record["evidence_id"],
            "evidence_sha256": record["evidence_sha256"],
            "available_at": _instant(
                record["available_at"],
                "settlement evidence available_at",
            ).isoformat(),
            "quote_outcomes_sha256": record.get("quote_outcomes_sha256"),
        }

    def _build_evidence_record(
        self,
        item: dict[str, str | None],
        *,
        sequence: int,
        previous_record_sha256: str,
    ) -> dict[str, Any]:
        quote_outcomes_sha256 = item.get("quote_outcomes_sha256")
        record: dict[str, Any] = {
            "schema": self._EVIDENCE_SCHEMA,
            "schema_version": (
                self._LEGACY_EVIDENCE_VERSION
                if quote_outcomes_sha256 is None
                else self._EVIDENCE_VERSION
            ),
            "sequence": sequence,
            "previous_record_sha256": previous_record_sha256,
            "evidence_key_sha256": self._evidence_key(item["evidence_id"]),
            "event_identity": item["event_identity"],
            "settlement_ref": item["settlement_ref"],
            "evidence_id": item["evidence_id"],
            "evidence_sha256": item["evidence_sha256"],
            "available_at": _instant(
                item["available_at"],
                "settlement evidence available_at",
            ).isoformat(),
        }
        if quote_outcomes_sha256 is not None:
            record["quote_outcomes_sha256"] = _sha256(
                quote_outcomes_sha256,
                "settlement evidence quote_outcomes_sha256",
            )
        record["record_sha256"] = self._canonical_sha256(record)
        return self._validate_evidence_record(record)

    def _load_evidence_history(
        self,
        state: dict[str, Any],
    ) -> tuple[dict[str, str | None], ...]:
        count = state["settlement_evidence_count"]
        paths = self._record_paths()
        if len(paths) != count:
            raise ContinuousSessionError(
                "settlement evidence journal cardinality mismatch"
            )
        if count == 0:
            return ()
        records = sorted(
            (self._read_evidence_path(path) for path in paths),
            key=lambda item: item["sequence"],
        )
        previous = self._EMPTY_EVIDENCE_TIP
        evidence_ids: set[str] = set()
        event_refs: set[tuple[str, str]] = set()
        for expected_sequence, record in enumerate(records, start=1):
            if record["sequence"] != expected_sequence:
                raise ContinuousSessionError(
                    "settlement evidence journal sequence is not contiguous"
                )
            if record["previous_record_sha256"] != previous:
                raise ContinuousSessionError(
                    "settlement evidence journal chain mismatch"
                )
            evidence_id = record["evidence_id"]
            if evidence_id in evidence_ids:
                raise ContinuousSessionError(
                    "settlement evidence journal repeats evidence_id"
                )
            evidence_ids.add(evidence_id)
            event_ref = (record["event_identity"], record["settlement_ref"])
            if event_ref in event_refs:
                raise ContinuousSessionError(
                    "settlement evidence journal repeats event/reference authority"
                )
            event_refs.add(event_ref)
            previous = record["record_sha256"]
        tip = records[-1]
        if (
            tip["record_sha256"] != state["settlement_evidence_tip_sha256"]
            or tip["evidence_key_sha256"]
            != state["settlement_evidence_tip_key_sha256"]
        ):
            raise ContinuousSessionError(
                "settlement evidence journal tip mismatch"
            )
        normalized = tuple(
            self._normalized_item_from_record(record)
            for record in records
        )
        # Preserve the v2 public status contract: retained settlement evidence was
        # materialized in global evidence_id order even though the v3 hash chain
        # itself must remain append-ordered for crash-safe incremental commits.
        return tuple(
            sorted(normalized, key=lambda item: item["evidence_id"])
        )

    def _verify_evidence_tip(self, state: dict[str, Any]) -> None:
        count = state["settlement_evidence_count"]
        if count == 0:
            if self.evidence_dir.is_symlink() or self.evidence_dir.exists():
                raise ContinuousSessionError(
                    "empty settlement evidence checkpoint has an orphan journal"
                )
            return
        if self.evidence_dir.is_symlink() or not self.evidence_dir.is_dir():
            raise ContinuousSessionError(
                "settlement evidence journal path is not a canonical directory"
            )
        path = self._evidence_path(
            state["settlement_evidence_tip_key_sha256"]
        )
        if not path.exists():
            raise ContinuousSessionError(
                "settlement evidence journal tip is missing"
            )
        record = self._read_evidence_path(path)
        if (
            record["sequence"] != count
            or record["record_sha256"]
            != state["settlement_evidence_tip_sha256"]
        ):
            raise ContinuousSessionError(
                "settlement evidence journal tip is inconsistent"
            )

    def _verify_pending_recovery_prefix(
        self,
        state: dict[str, Any],
        pending: dict[str, Any],
    ) -> None:
        """Verify committed history plus any exact already-written pending prefix."""

        base_count = pending["base_count"]
        pending_records = tuple(pending["records"])
        paths = self._record_paths()
        if (
            len(paths) < base_count
            or len(paths) > base_count + len(pending_records)
        ):
            raise ContinuousSessionError(
                "settlement evidence journal cardinality conflicts with pending recovery"
            )

        records = sorted(
            (self._read_evidence_path(path) for path in paths),
            key=lambda item: item["sequence"],
        )
        previous = self._EMPTY_EVIDENCE_TIP
        evidence_ids: set[str] = set()
        event_refs: set[tuple[str, str]] = set()
        for expected_sequence, record in enumerate(records, start=1):
            if record["sequence"] != expected_sequence:
                raise ContinuousSessionError(
                    "settlement evidence recovery sequence is not contiguous"
                )
            if record["previous_record_sha256"] != previous:
                raise ContinuousSessionError(
                    "settlement evidence recovery chain mismatch"
                )
            evidence_id = record["evidence_id"]
            if evidence_id in evidence_ids:
                raise ContinuousSessionError(
                    "settlement evidence recovery repeats evidence_id"
                )
            evidence_ids.add(evidence_id)
            event_ref = (record["event_identity"], record["settlement_ref"])
            if event_ref in event_refs:
                raise ContinuousSessionError(
                    "settlement evidence recovery repeats event/reference authority"
                )
            event_refs.add(event_ref)

            if expected_sequence > base_count:
                pending_offset = expected_sequence - base_count - 1
                if record != pending_records[pending_offset]:
                    raise ContinuousSessionError(
                        "settlement evidence recovery tail conflicts with pending transaction"
                    )
            previous = record["record_sha256"]

        already_written_pending = max(0, len(records) - base_count)
        for record in pending_records[already_written_pending:]:
            evidence_id = record["evidence_id"]
            if evidence_id in evidence_ids:
                raise ContinuousSessionError(
                    "settlement evidence recovery pending tail repeats evidence_id"
                )
            evidence_ids.add(evidence_id)
            event_ref = (record["event_identity"], record["settlement_ref"])
            if event_ref in event_refs:
                raise ContinuousSessionError(
                    "settlement evidence recovery pending tail repeats event/reference authority"
                )
            event_refs.add(event_ref)

        if base_count == 0:
            if records:
                first = records[0]
                if first != pending_records[0]:
                    raise ContinuousSessionError(
                        "settlement evidence recovery has an unexpected initial record"
                    )
            return

        if len(records) < base_count:
            raise ContinuousSessionError(
                "settlement evidence recovery base history is incomplete"
            )
        base_tip = records[base_count - 1]
        if (
            base_tip["record_sha256"]
            != state["settlement_evidence_tip_sha256"]
            or base_tip["evidence_key_sha256"]
            != state["settlement_evidence_tip_key_sha256"]
        ):
            raise ContinuousSessionError(
                "settlement evidence recovery base tip mismatch"
            )

    def _state_from_embedded(
        self,
        raw: dict[str, Any],
        records: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        state = {
            "schema": self._SCHEMA,
            "schema_version": self._VERSION,
        }
        for name in self._STATE_FIELDS:
            value = raw[name]
            state[name] = (
                list(value)
                if name == "source_unresolved_gap_delta_ids"
                else value
            )
        state["settlement_evidence_count"] = len(records)
        state["settlement_evidence_tip_sha256"] = (
            records[-1]["record_sha256"]
            if records
            else self._EMPTY_EVIDENCE_TIP
        )
        state["settlement_evidence_tip_key_sha256"] = (
            records[-1]["evidence_key_sha256"]
            if records
            else self._EMPTY_EVIDENCE_TIP
        )
        state["settlement_evidence_pending"] = None
        return self._validate_v4(state)

    def _migrate_embedded_locked(
        self,
        raw: dict[str, Any],
    ) -> dict[str, Any]:
        if raw["schema_version"] not in {
            self._V2_VERSION,
            self._V3_VERSION,
        }:
            raise ContinuousSessionError(
                "unsupported embedded continuous session migration source"
            )
        evidence = tuple(
            sorted(
                (dict(item) for item in raw["settlement_evidence"]),
                key=lambda item: item["evidence_id"],
            )
        )
        records: list[dict[str, Any]] = []
        previous = self._EMPTY_EVIDENCE_TIP
        for sequence, item in enumerate(evidence, start=1):
            record = self._build_evidence_record(
                item,
                sequence=sequence,
                previous_record_sha256=previous,
            )
            records.append(record)
            previous = record["record_sha256"]

        expected_by_name = {
            f"{record['evidence_key_sha256']}.json": record
            for record in records
        }
        if not records and (
            self.evidence_dir.is_symlink() or self.evidence_dir.exists()
        ):
            raise ContinuousSessionError(
                "empty embedded settlement evidence conflicts with existing journal"
            )
        for path in self._record_paths():
            expected = expected_by_name.get(path.name)
            if expected is None or self._read_evidence_path(path) != expected:
                raise ContinuousSessionError(
                    "embedded continuous session conflicts with existing evidence journal"
                )
        for record in records:
            self._write_evidence_record(record)

        state = self._state_from_embedded(raw, tuple(records))
        atomic_write_json(self.path, state)
        persisted = self._read_file()
        if persisted["schema_version"] != self._VERSION:
            raise ContinuousSessionError(
                "continuous session migration did not publish schema v4"
            )
        self._load_evidence_history(persisted)
        return persisted

    def _write_state_locked(self, raw: dict[str, Any]) -> dict[str, Any]:
        candidate = self._validate_v4(dict(raw))
        atomic_write_json(self.path, candidate)
        persisted = self._read_file()
        if persisted["schema_version"] != self._VERSION:
            raise ContinuousSessionError(
                "continuous session state publication regressed schema version"
            )
        self._verify_evidence_tip(persisted)
        return persisted

    def _recover_pending_locked(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        pending = state["settlement_evidence_pending"]
        if pending is None:
            self._verify_evidence_tip(state)
            return state
        pending = self._validate_pending(pending, state)
        # Recovery crosses a durability boundary: validate the entire committed
        # base plus any exact pending prefix already published by an interrupted
        # prior recovery before writing more. This exceptional O(history) check
        # does not reintroduce history-proportional ordinary checkpoint work.
        self._verify_pending_recovery_prefix(state, pending)
        for record in pending["records"]:
            self._write_evidence_record(record)
        final = dict(state)
        final["settlement_evidence_count"] = (
            pending["base_count"] + len(pending["records"])
        )
        final["settlement_evidence_tip_sha256"] = pending["records"][-1][
            "record_sha256"
        ]
        final["settlement_evidence_tip_key_sha256"] = pending["records"][-1][
            "evidence_key_sha256"
        ]
        final["settlement_evidence_pending"] = None
        final["cycles_completed"] = int(final["cycles_completed"]) + 1
        final["last_success_at"] = _instant(
            pending["success_at"],
            "settlement evidence pending success_at",
        ).isoformat()
        final["last_error_code"] = None
        if pending["full_refresh"]:
            final["last_full_refresh_at"] = final["last_success_at"]
        persisted = self._write_state_locked(final)
        self._load_evidence_history(persisted)
        return persisted

    def _current_state_locked(self) -> dict[str, Any]:
        raw = self._read_file()
        if raw["schema_version"] != self._VERSION:
            raise ContinuousSessionError(
                "continuous session state rolled back to legacy schema during runtime"
            )
        raw = self._recover_pending_locked(raw)
        self._verify_evidence_tip(raw)
        return raw

    def _read_current_state(self) -> dict[str, Any]:
        with durable_path_lock(self.path):
            return self._current_state_locked()

    def _update_state(
        self,
        mutate: Callable[[dict[str, Any]], None],
    ) -> None:
        with durable_path_lock(self.path):
            raw = self._current_state_locked()
            protected = (
                raw["settlement_evidence_count"],
                raw["settlement_evidence_tip_sha256"],
                raw["settlement_evidence_tip_key_sha256"],
                raw["settlement_evidence_pending"],
            )
            mutate(raw)
            if protected != (
                raw["settlement_evidence_count"],
                raw["settlement_evidence_tip_sha256"],
                raw["settlement_evidence_tip_key_sha256"],
                raw["settlement_evidence_pending"],
            ):
                raise ContinuousSessionError(
                    "operational mutation changed settlement evidence authority"
                )
            self._write_state_locked(raw)

    def snapshot(self) -> ContinuousSessionStatus:
        with durable_path_lock(self.path):
            raw = self._current_state_locked()
            evidence = self._load_evidence_history(raw)
            return ContinuousSessionStatus(
                session_id=raw["session_id"],
                source_id=raw["source_id"],
                state=SessionState(raw["state"]),
                cycles_completed=raw["cycles_completed"],
                last_success_at=raw["last_success_at"],
                last_error_code=raw["last_error_code"],
                last_full_refresh_at=raw["last_full_refresh_at"],
                settlement_evidence=tuple(dict(item) for item in evidence),
                settlement_evidence_count=raw["settlement_evidence_count"],
                settlement_evidence_materialized=True,
                source_gap_state=raw["source_gap_state"],
                source_sync_state=raw["source_sync_state"],
                source_state_delta_id=raw["source_state_delta_id"],
                source_unresolved_gap_delta_ids=tuple(
                    raw["source_unresolved_gap_delta_ids"]
                ),
                source_projection_stream_epoch=raw[
                    "source_projection_stream_epoch"
                ],
                source_state_projection_backlog=raw[
                    "source_state_projection_backlog"
                ],
            )

    def operational_snapshot(self) -> ContinuousSessionStatus:
        raw = self._read_current_state()
        return ContinuousSessionStatus(
            session_id=raw["session_id"],
            source_id=raw["source_id"],
            state=SessionState(raw["state"]),
            cycles_completed=raw["cycles_completed"],
            last_success_at=raw["last_success_at"],
            last_error_code=raw["last_error_code"],
            last_full_refresh_at=raw["last_full_refresh_at"],
            settlement_evidence=(),
            settlement_evidence_count=raw["settlement_evidence_count"],
            settlement_evidence_materialized=False,
            source_gap_state=raw["source_gap_state"],
            source_sync_state=raw["source_sync_state"],
            source_state_delta_id=raw["source_state_delta_id"],
            source_unresolved_gap_delta_ids=tuple(
                raw["source_unresolved_gap_delta_ids"]
            ),
            source_projection_stream_epoch=raw[
                "source_projection_stream_epoch"
            ],
            source_state_projection_backlog=raw[
                "source_state_projection_backlog"
            ],
        )

    @property
    def session_id(self) -> str:
        return self._read_current_state()["session_id"]

    def set_state(self, state: SessionState, *, reason: str | None = None) -> None:
        if not isinstance(state, SessionState):
            raise TypeError("state must be SessionState")

        def mutate(raw: dict[str, Any]) -> None:
            raw["state"] = state.value
            if reason is not None:
                raw["last_error_code"] = _text(reason, "reason")

        self._update_state(mutate)

    @staticmethod
    def _quote_outcomes_sha256(evidence: SettlementResolution) -> str:
        evidence.validate(as_of=evidence.available_at)
        return _settlement_quote_outcomes_sha256(evidence.quote_outcomes)

    @staticmethod
    def _normalized_settlement_evidence(
        evidence: SettlementResolution,
    ) -> dict[str, str | None]:
        return {
            "event_identity": evidence.event_identity,
            "settlement_ref": evidence.settlement_ref,
            "evidence_id": evidence.evidence_id,
            "evidence_sha256": evidence.evidence_sha256,
            "available_at": _instant(
                evidence.available_at,
                "available_at",
            ).isoformat(),
            "quote_outcomes_sha256": _ContinuousSessionState._quote_outcomes_sha256(
                evidence
            ),
        }

    def validate_settlement_evidence(
        self,
        *,
        settlement_evidence: tuple[SettlementResolution, ...],
    ) -> None:
        if not settlement_evidence:
            # This pre-effect trust boundary must still verify the bounded
            # checkpoint and committed journal tip. It deliberately avoids
            # materializing historical receipts, but it must not let a tick
            # proceed into settlement-learning side effects over corrupt
            # durable authority.
            self._read_current_state()
            return
        with durable_path_lock(self.path):
            raw = self._current_state_locked()
            history = self._load_evidence_history(raw)
            known = {item["evidence_id"]: item for item in history}
            known_event_refs = {
                (item["event_identity"], item["settlement_ref"]): item
                for item in history
            }
            pending_ids: dict[str, dict[str, str | None]] = {}
            pending_event_refs: dict[
                tuple[str, str], dict[str, str | None]
            ] = {}
            for evidence in settlement_evidence:
                normalized = self._normalized_settlement_evidence(evidence)
                prior = pending_ids.get(evidence.evidence_id)
                if prior is not None and prior != normalized:
                    raise ContinuousSessionError(
                        "settlement evidence id conflicts within current batch"
                    )
                pending_ids[evidence.evidence_id] = normalized
                event_ref = (evidence.event_identity, evidence.settlement_ref)
                prior_event_ref = pending_event_refs.get(event_ref)
                if prior_event_ref is not None and prior_event_ref != normalized:
                    raise ContinuousSessionError(
                        "settlement event/reference conflicts within current batch"
                    )
                pending_event_refs[event_ref] = normalized
                existing_event_ref = known_event_refs.get(event_ref)
                if existing_event_ref is not None and existing_event_ref != normalized:
                    raise ContinuousSessionError(
                        "settlement event/reference conflicts with durable evidence"
                    )
                existing = known.get(evidence.evidence_id)
                if existing is not None:
                    if existing["quote_outcomes_sha256"] is None:
                        raise ContinuousSessionError(
                            "legacy settlement evidence cannot be safely rebound without "
                            "an outcome fingerprint"
                        )
                    if existing != normalized:
                        raise ContinuousSessionError(
                            "settlement evidence id conflicts with durable evidence"
                        )

    def record_source_projection(
        self,
        *,
        deltas: tuple[CollectorDelta, ...],
        backlog: bool,
    ) -> None:
        if type(backlog) is not bool:
            raise TypeError("backlog must be boolean")
        for delta in deltas:
            if not isinstance(delta, CollectorDelta):
                raise TypeError("deltas must contain CollectorDelta values")
            delta.validate()
            if delta.source_id != self.source_id:
                raise ContinuousSessionError(
                    "source-state projection delta belongs to another source"
                )

        def mutate(raw: dict[str, Any]) -> None:
            unresolved = set(raw["source_unresolved_gap_delta_ids"])
            projection_epoch = raw["source_projection_stream_epoch"]
            for delta in deltas:
                if projection_epoch != delta.stream_epoch:
                    unresolved.clear()
                    projection_epoch = delta.stream_epoch
                if delta.gap_state is GapState.DETECTED:
                    unresolved.add(delta.delta_id)
                elif delta.gap_state is GapState.RECOVERED:
                    if delta.revision_of is None:
                        raise ContinuousSessionError(
                            "recovered gap projection requires revision_of"
                        )
                    unresolved.discard(delta.revision_of)
                elif delta.gap_state is GapState.CURSOR_RESET:
                    unresolved.clear()

                raw["source_state_delta_id"] = delta.delta_id
                raw["source_projection_stream_epoch"] = projection_epoch
                if unresolved:
                    raw["source_gap_state"] = GapState.DETECTED.value
                    raw["source_sync_state"] = SyncState.GAP_DETECTED.value
                else:
                    raw["source_gap_state"] = delta.gap_state.value
                    raw["source_sync_state"] = delta.sync_state.value

            raw["source_unresolved_gap_delta_ids"] = sorted(unresolved)
            raw["source_state_projection_backlog"] = backlog

        self._update_state(mutate)

    def record_success(
        self,
        *,
        at: str,
        full_refresh: bool,
        settlement_evidence: tuple[SettlementResolution, ...],
    ) -> None:
        timestamp = _instant(at, "at").isoformat()
        if type(full_refresh) is not bool:
            raise TypeError("full_refresh must be boolean")

        def mutate_success(raw: dict[str, Any]) -> None:
            raw["cycles_completed"] = int(raw["cycles_completed"]) + 1
            raw["last_success_at"] = timestamp
            raw["last_error_code"] = None
            if full_refresh:
                raw["last_full_refresh_at"] = timestamp

        if not settlement_evidence:
            self._update_state(mutate_success)
            return

        with durable_path_lock(self.path):
            raw = self._current_state_locked()
            history = self._load_evidence_history(raw)
            known = {item["evidence_id"]: item for item in history}
            known_event_refs = {
                (item["event_identity"], item["settlement_ref"]): item
                for item in history
            }
            incoming: dict[str, dict[str, str | None]] = {}
            incoming_event_refs: dict[
                tuple[str, str], dict[str, str | None]
            ] = {}
            for evidence in settlement_evidence:
                normalized = self._normalized_settlement_evidence(evidence)
                prior = incoming.get(evidence.evidence_id)
                if prior is not None and prior != normalized:
                    raise ContinuousSessionError(
                        "settlement evidence id conflicts within current batch"
                    )
                incoming[evidence.evidence_id] = normalized
                event_ref = (evidence.event_identity, evidence.settlement_ref)
                prior_event_ref = incoming_event_refs.get(event_ref)
                if prior_event_ref is not None and prior_event_ref != normalized:
                    raise ContinuousSessionError(
                        "settlement event/reference conflicts within current batch"
                    )
                incoming_event_refs[event_ref] = normalized
                existing_event_ref = known_event_refs.get(event_ref)
                if existing_event_ref is not None and existing_event_ref != normalized:
                    raise ContinuousSessionError(
                        "settlement event/reference conflicts with durable evidence"
                    )
                existing = known.get(evidence.evidence_id)
                if existing is not None:
                    if existing["quote_outcomes_sha256"] is None:
                        raise ContinuousSessionError(
                            "legacy settlement evidence cannot be safely rebound without "
                            "an outcome fingerprint"
                        )
                    if existing != normalized:
                        raise ContinuousSessionError(
                            "settlement evidence id conflicts with durable evidence"
                        )

            new_items = tuple(
                incoming[evidence_id]
                for evidence_id in sorted(incoming)
                if evidence_id not in known
            )
            if not new_items:
                mutate_success(raw)
                self._write_state_locked(raw)
                return

            previous = raw["settlement_evidence_tip_sha256"]
            records: list[dict[str, Any]] = []
            for offset, item in enumerate(new_items, start=1):
                record = self._build_evidence_record(
                    item,
                    sequence=raw["settlement_evidence_count"] + offset,
                    previous_record_sha256=previous,
                )
                records.append(record)
                previous = record["record_sha256"]

            prepared = dict(raw)
            prepared["settlement_evidence_pending"] = {
                "base_count": raw["settlement_evidence_count"],
                "base_tip_sha256": raw[
                    "settlement_evidence_tip_sha256"
                ],
                "success_at": timestamp,
                "full_refresh": full_refresh,
                "records": records,
            }
            prepared = self._write_state_locked(prepared)
            self._recover_pending_locked(prepared)

    def record_failure(self, *, code: str) -> None:
        code = _text(code, "code")
        self._update_state(
            lambda raw: raw.__setitem__("last_error_code", code)
        )


class ContinuousSessionCoordinator(metaclass=_ContinuousSessionCoordinatorMeta):
    """Compose existing collector/lifecycle/mirror/settlement authorities into one durable loop.

    The coordinator owns only session identity/checkpoint and sequencing. It never becomes
    a market store, delta store, lifecycle store, scheduler, outcome authority, or LLM
    decision engine.
    """

    _settlement_consumer_bindings_sealed = False

    def __init__(
        self,
        *,
        workspace: str | Path,
        collector: HeadlessCollectorService,
        lifecycle: ContinuousEventLifecycle,
        market_store: SQLiteMarketStore,
        desktop_consumer: DesktopDeltaConsumer,
        invalidation_buffer: BoundedMirrorInvalidationBuffer,
        dependency_index: FocusedMirrorDependencyIndex,
        paper_book_path: str | Path | None = None,
        outcome_authority: SettlementOutcomeAuthority | None = None,
        settlement_learning_handoff: SettlementLearningHandoff | None = None,
        session_id: str | None = None,
        clock: Callable[[], str] | None = None,
        required_history: timedelta = timedelta(0),
        max_invalidation_batches_per_tick: int = 4,
        max_invalidation_items_per_batch: int = 250,
        causal_view: CausalView = CausalView.AS_KNOWN_AT_DECISION,
        initial_bankroll: str = "10000",
    ) -> None:
        if not isinstance(workspace, (str, Path)):
            raise TypeError("workspace must be a path-like value")
        if not isinstance(collector, HeadlessCollectorService):
            raise TypeError("collector must be HeadlessCollectorService")
        if not isinstance(lifecycle, ContinuousEventLifecycle):
            raise TypeError("lifecycle must be ContinuousEventLifecycle")
        if not isinstance(market_store, SQLiteMarketStore):
            raise TypeError("market_store must be SQLiteMarketStore")
        if not isinstance(desktop_consumer, DesktopDeltaConsumer):
            raise TypeError("desktop_consumer must be DesktopDeltaConsumer")
        if not isinstance(invalidation_buffer, BoundedMirrorInvalidationBuffer):
            raise TypeError(
                "invalidation_buffer must be BoundedMirrorInvalidationBuffer"
            )
        if not isinstance(dependency_index, FocusedMirrorDependencyIndex):
            raise TypeError("dependency_index must be FocusedMirrorDependencyIndex")
        if outcome_authority is not None and not callable(
            getattr(outcome_authority, "resolve", None)
        ):
            raise TypeError("outcome_authority.resolve must be callable")
        if settlement_learning_handoff is not None:
            if not callable(
                getattr(settlement_learning_handoff, "reconcile_after_settlement", None)
            ):
                raise TypeError(
                    "settlement_learning_handoff.reconcile_after_settlement must be callable"
                )
            prepare = getattr(settlement_learning_handoff, "prepare_settlement", None)
            if prepare is not None and not callable(prepare):
                raise TypeError(
                    "settlement_learning_handoff.prepare_settlement must be callable"
                )

        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.collector = collector
        self.lifecycle = lifecycle
        self.market_store = market_store
        self.desktop_consumer = desktop_consumer
        self.invalidation_buffer = invalidation_buffer
        self.dependency_index = dependency_index
        self.paper_book_path = (
            self.workspace / "paper_book.json"
            if paper_book_path is None
            else Path(paper_book_path)
        )
        self.outcome_authority = outcome_authority
        self.settlement_learning_handoff = settlement_learning_handoff
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        if isinstance(required_history, timedelta) and required_history.total_seconds() < 0:
            raise ValueError("required_history cannot be negative")
        if not isinstance(required_history, timedelta):
            raise TypeError("required_history must be timedelta")
        self.required_history = required_history
        for name, value in (
            ("max_invalidation_batches_per_tick", max_invalidation_batches_per_tick),
            ("max_invalidation_items_per_batch", max_invalidation_items_per_batch),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer")
        self.max_invalidation_batches_per_tick = max_invalidation_batches_per_tick
        self.max_invalidation_items_per_batch = max_invalidation_items_per_batch
        try:
            self.causal_view = CausalView(causal_view)
        except ValueError as exc:
            raise ValueError("unsupported causal_view") from exc
        try:
            initial_bankroll = str(initial_bankroll)
            PaperBook(initial_bankroll)
        except Exception as exc:
            raise ValueError("initial_bankroll must construct a valid PaperBook") from exc
        self.initial_bankroll = initial_bankroll
        self._state = _ContinuousSessionState(
            self.workspace / "continuous_session.json",
            session_id=session_id,
            source_id=collector.source_id,
            clock=self.clock,
        )

    @property
    def session_id(self) -> str:
        return self._state.session_id

    def _status_from_snapshot(
        self,
        snapshot: ContinuousSessionStatus,
    ) -> ContinuousSessionStatus:
        if not isinstance(snapshot, ContinuousSessionStatus):
            raise TypeError("snapshot must be ContinuousSessionStatus")
        source_status = self.collector.status()
        source_last_success = source_status.get("last_success_at")
        source_last_error = source_status.get("last_error_code")
        if source_last_success is not None and not isinstance(source_last_success, str):
            raise ContinuousSessionError("collector last_success_at must be a string or None")
        if source_last_error is not None and not isinstance(source_last_error, str):
            raise ContinuousSessionError("collector last_error_code must be a string or None")
        return replace(
            snapshot,
            source_provider_unavailable=source_last_error == "ProviderUnavailableError",
            source_last_success_at=source_last_success,
            source_last_error_code=source_last_error,
            source_gap_state=snapshot.source_gap_state,
            source_sync_state=snapshot.source_sync_state,
            source_state_delta_id=snapshot.source_state_delta_id,
            source_unresolved_gap_delta_ids=snapshot.source_unresolved_gap_delta_ids,
            source_projection_stream_epoch=snapshot.source_projection_stream_epoch,
            source_state_projection_backlog=snapshot.source_state_projection_backlog,
            invalidation_pending_count=self.invalidation_buffer.pending_count,
            invalidation_full_refresh_required=bool(
                self.invalidation_buffer.full_refresh_required
            ),
        )

    def status(self) -> ContinuousSessionStatus:
        """Return full audit status, including verified settlement history."""
        return self._status_from_snapshot(self._state.snapshot())

    def operational_status(self) -> ContinuousSessionStatus:
        """Return bounded lifecycle/source status without scanning settlement history."""
        return self._status_from_snapshot(self._state.operational_snapshot())

    def pause(self) -> None:
        self._state.set_state(SessionState.PAUSED)

    def stop(self, reason: str = "operator_stop") -> None:
        self._state.set_state(SessionState.STOPPED, reason=reason)

    def resume(self) -> None:
        current = self._state.operational_snapshot().state
        if current not in {SessionState.PAUSED, SessionState.STOPPED}:
            return
        self._state.set_state(SessionState.RUNNING)

    def _require_running(self) -> None:
        state = self._state.operational_snapshot().state
        if state is SessionState.PAUSED:
            raise SessionPausedError("continuous session is durably PAUSED")
        if state is SessionState.STOPPED:
            raise SessionStoppedError("continuous session is durably STOPPED")

    def _register_input(self, input_id: str, **selectors: object) -> None:
        if input_id in self.dependency_index.input_ids:
            return
        self.dependency_index.register(input_id, **selectors)

    def _retire_input(self, input_id: str) -> None:
        self.dependency_index.unregister(input_id)

    def _drain_invalidations(self) -> tuple[
        tuple[str, ...],
        bool,
        bool,
    ]:
        affected: list[str] = []
        full_refresh_required = False
        backlog = False

        for _ in range(self.max_invalidation_batches_per_tick):
            batch = self.invalidation_buffer.drain(
                max_items=self.max_invalidation_items_per_batch
            )
            if not isinstance(batch, MirrorInvalidationBatch):
                raise ContinuousSessionError(
                    "invalidation buffer returned an invalid batch"
                )
            routed = self.dependency_index.affected_inputs(batch)
            affected.extend(routed)
            full_refresh_required = full_refresh_required or batch.full_refresh_required
            if not batch.has_more:
                break
        else:
            backlog = self.invalidation_buffer.pending_count > 0 or (
                self.invalidation_buffer.full_refresh_required
            )
        return tuple(dict.fromkeys(affected)), full_refresh_required, backlog

    def _refresh_source_state_projection(self) -> ContinuousSessionStatus:
        snapshot = self._state.operational_snapshot()
        deltas = self.collector.delta_store.deltas_after_commit(
            source_id=self.collector.source_id,
            after_delta_id=snapshot.source_state_delta_id,
            max_items=self.collector.config.max_items + 1,
        )
        backlog = len(deltas) > self.collector.config.max_items
        selected = deltas[: self.collector.config.max_items]
        self._state.record_source_projection(
            deltas=selected,
            backlog=backlog,
        )
        return self._state.operational_snapshot()

    @_bind_canonical_settlement_snapshot
    def _settlement_resolutions(
        self,
        *,
        as_of: str,
        _snapshot_fn: Callable[..., tuple[SettlementResolution, ...]],
        _object_getattribute: Callable[[object, str], object],
        _capture_callback_fn: Callable[..., tuple[object | None, tuple[object, ...] | None]],
        _require_callback_fn: Callable[..., None],
        _captured_outcome_authority: object | None = None,
        _captured_outcome_resolve: object | None = None,
        _captured_outcome_resolve_witness: tuple[object, ...] | None = None,
    ) -> tuple[SettlementResolution, ...]:
        del _object_getattribute
        outcome_authority = self.outcome_authority
        if outcome_authority is None:
            if (
                _captured_outcome_authority is not None
                or _captured_outcome_resolve is not None
                or _captured_outcome_resolve_witness is not None
            ):
                raise ContinuousSessionError(
                    "settlement outcome authority changed during tick"
                )
            return ()
        if (
            _captured_outcome_authority is None
            and _captured_outcome_resolve is None
            and _captured_outcome_resolve_witness is None
        ):
            resolve, resolve_witness = _capture_callback_fn(
                outcome_authority,
                "resolve",
            )
        else:
            if _captured_outcome_authority is not outcome_authority:
                raise ContinuousSessionError(
                    "settlement outcome authority changed during tick"
                )
            resolve = _captured_outcome_resolve
            resolve_witness = _captured_outcome_resolve_witness
        if resolve is None or resolve_witness is None:
            raise ContinuousSessionError(
                "settlement outcome authority resolve dispatch must remain callable"
            )

        # Lifecycle record enumeration is callback-capable product work. Materialize
        # it completely, then revalidate the exact callback witness before any outcome
        # dispatch so lifecycle code cannot retarget settlement authority in between
        # tick's preflight and the actual resolve call.
        records = [record for record in self.lifecycle.records()]
        _require_callback_fn(
            outcome_authority,
            "resolve",
            resolve,
            resolve_witness,
            label="outcome authority resolve",
            relookup=False,
        )

        resolutions: list[SettlementResolution] = []
        for record in records:
            if record.phase is not EventPhase.COMPLETED or record.settlement_ref is None:
                continue
            if self.outcome_authority is not outcome_authority:
                raise ContinuousSessionError(
                    "settlement outcome authority changed during resolution"
                )
            _require_callback_fn(
                outcome_authority,
                "resolve",
                resolve,
                resolve_witness,
                label="outcome authority resolve",
            )
            resolution = resolve(record, as_of=as_of)
            # The callback is allowed to compute a resolution, not to retarget the
            # interpreter primitives used by canonical settlement validation,
            # snapshotting, PaperBook materialization, or the callback witness itself.
            # Revalidate before any post-callback product dispatch can consume them.
            _require_callback_fn(
                outcome_authority,
                "resolve",
                resolve,
                resolve_witness,
                label="outcome authority resolve",
            )
            if self.outcome_authority is not outcome_authority:
                raise ContinuousSessionError(
                    "settlement outcome authority changed during resolution"
                )
            if resolution is None:
                continue
            if type(resolution) is not SettlementResolution:
                raise ContinuousSessionError(
                    "outcome authority must return exact SettlementResolution or None"
                )
            if resolution.event_identity != record.identity:
                raise ContinuousSessionError(
                    "settlement evidence event identity does not match lifecycle identity"
                )
            if resolution.settlement_ref != record.settlement_ref:
                raise ContinuousSessionError(
                    "settlement evidence reference does not match lifecycle evidence"
                )
            resolution.validate(as_of=as_of)
            (snapshot,) = _snapshot_fn(
                (resolution,),
                as_of=as_of,
            )
            # The detached object, not the authority-owned object, is the final
            # lifecycle-bound truth crossing into session economics.
            if snapshot.event_identity != record.identity:
                raise ContinuousSessionError(
                    "settlement snapshot event identity does not match lifecycle identity"
                )
            if snapshot.settlement_ref != record.settlement_ref:
                raise ContinuousSessionError(
                    "settlement snapshot reference does not match lifecycle evidence"
                )
            resolutions.append(snapshot)
        return tuple(resolutions)

    def _load_book(self) -> PaperBook:
        if self.paper_book_path.exists():
            return PaperBook.load(self.paper_book_path)
        return PaperBook(self.initial_bankroll)

    _settlement_handoff_snapshot = staticmethod(_canonical_settlement_handoff_snapshot)

    @_seal_settlement_consumer_entry
    @_bind_canonical_settlement_engine
    def _settle(
        self,
        *,
        resolutions: tuple[SettlementResolution, ...],
        _settlement_engine_type: type[SettlementEngine],
        _economic_lock_type: type[WorkspaceEconomicLock],
        _snapshot_fn: Callable[..., tuple[SettlementResolution, ...]],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not resolutions:
            return (), ()
        if SettlementEngine is not _settlement_engine_type:
            raise ContinuousSessionError("settlement engine constructor origin changed")
        if WorkspaceEconomicLock is not _economic_lock_type:
            raise ContinuousSessionError("settlement economic lock origin changed")
        execution_resolutions = _snapshot_fn(resolutions)
        unique: dict[str, SettlementResolution] = {}
        for resolution in execution_resolutions:
            unique.setdefault(resolution.evidence_id, resolution)

        lock = _economic_lock_type(self.workspace)
        if type(lock) is not _economic_lock_type:
            raise ContinuousSessionError("settlement economic lock constructor origin changed")
        with lock:
            book = self._load_book()
            engine = _settlement_engine_type()
            if type(engine) is not _settlement_engine_type:
                raise ContinuousSessionError("settlement engine constructor returned non-canonical type")
            for resolution in unique.values():
                self._require_settlement_causal_for_open_tickets(book, resolution)
                allowed = self._open_quote_keys_for_book(book, resolution.event_identity)
                scoped = {
                    quote_key: outcome
                    for quote_key, outcome in resolution.quote_outcomes.items()
                    if quote_key in allowed
                }
                if scoped:
                    engine.record(scoped)
            settled = tuple(engine.settle_ready(book))
            if settled:
                book.save(self.paper_book_path)

        return settled, tuple(unique)

    @staticmethod
    def _require_settlement_causal_for_open_tickets(
        book: PaperBook,
        resolution: SettlementResolution,
    ) -> None:
        available_at = _instant(
            resolution.available_at,
            "settlement available_at",
        )
        event_parts = {resolution.event_identity}
        if ":" in resolution.event_identity:
            event_parts.add(resolution.event_identity.split(":", 1)[1])
        resolution_quote_keys = set(resolution.quote_outcomes)

        for ticket in book.tickets.values():
            if ticket.status.value != "open":
                continue
            matches_resolution = any(
                leg.event_id in event_parts
                and leg.quote_key in resolution_quote_keys
                for leg in ticket.legs
            )
            if not matches_resolution:
                continue
            placed_at = _instant(
                ticket.placed_at,
                f"ticket {ticket.ticket_id} placed_at",
            )
            if available_at < placed_at:
                raise ContinuousSessionError(
                    "settlement evidence predates matching open ticket placement"
                )

    @staticmethod
    def _open_quote_keys_for_book(
        book: PaperBook,
        event_identity: str,
    ) -> set[str]:
        parts = {event_identity}
        if ":" in event_identity:
            parts.add(event_identity.split(":", 1)[1])
        return {
            leg.quote_key
            for ticket in book.tickets.values()
            if ticket.status.value == "open"
            for leg in ticket.legs
            if leg.event_id in parts
        }

    @_bind_canonical_settlement_snapshot
    def tick(
        self,
        *,
        _snapshot_fn: Callable[..., tuple[SettlementResolution, ...]],
        _object_getattribute: Callable[[object, str], object],
        _capture_callback_fn: Callable[..., tuple[object | None, tuple[object, ...] | None]],
        _require_callback_fn: Callable[..., None],
    ) -> ContinuousTickResult:
        self._require_running()

        # One tick must use one configured outcome authority and learning handoff.
        # Callback-capable collector/lifecycle/outcome work may not retarget either
        # collaborator before settlement truth reaches learning or PAPER economics.
        outcome_authority = self.outcome_authority
        learning_handoff = self.settlement_learning_handoff

        del _object_getattribute
        capture_callback = _capture_callback_fn
        require_callback = _require_callback_fn

        outcome_resolve = None
        outcome_resolve_witness = None
        if outcome_authority is not None:
            outcome_resolve, outcome_resolve_witness = capture_callback(
                outcome_authority,
                "resolve",
            )

        learning_prepare = None
        learning_prepare_witness = None
        learning_reconcile = None
        learning_reconcile_witness = None
        if learning_handoff is not None:
            learning_prepare, learning_prepare_witness = capture_callback(
                learning_handoff,
                "prepare_settlement",
                optional=True,
            )
            learning_reconcile, learning_reconcile_witness = capture_callback(
                learning_handoff,
                "reconcile_after_settlement",
            )

        def require_learning_handoff() -> None:
            if self.outcome_authority is not outcome_authority:
                raise ContinuousSessionError(
                    "settlement outcome authority changed during tick"
                )
            if outcome_authority is not None:
                require_callback(
                    outcome_authority,
                    "resolve",
                    outcome_resolve,
                    outcome_resolve_witness,
                    label="outcome authority resolve",
                )
            if self.settlement_learning_handoff is not learning_handoff:
                raise ContinuousSessionError(
                    "settlement learning handoff changed during tick"
                )
            if learning_handoff is not None:
                require_callback(
                    learning_handoff,
                    "prepare_settlement",
                    learning_prepare,
                    learning_prepare_witness,
                    label="learning prepare",
                    optional=True,
                )
                require_callback(
                    learning_handoff,
                    "reconcile_after_settlement",
                    learning_reconcile,
                    learning_reconcile_witness,
                    label="learning reconcile",
                )

        now = self.clock()
        _instant(now, "now")
        try:
            cycle = self.collector.run_cycle()
            source_snapshot = self._refresh_source_state_projection()
            require_learning_handoff()
            if cycle.provider_unavailable:
                self._state.record_failure(code="ProviderUnavailableError")
                snapshot = self._state.operational_snapshot()
                return ContinuousTickResult(
                    session_id=self.session_id,
                    cycle_index=snapshot.cycles_completed,
                    source_id=cycle.source_id,
                    source_provider_unavailable=True,
                    source_gap_states=(
                        ()
                        if source_snapshot.source_gap_state is None
                        else (source_snapshot.source_gap_state,)
                    ),
                    source_sync_states=(
                        ()
                        if source_snapshot.source_sync_state is None
                        else (source_snapshot.source_sync_state,)
                    ),
                    committed_delta_ids=cycle.committed_delta_ids,
                    delivered_delta_ids=(),
                    affected_input_ids=(),
                    registered_input_ids=(),
                    retired_input_ids=(),
                    full_refresh_required=bool(
                        self.invalidation_buffer.full_refresh_required
                    ),
                    invalidation_backlog=(
                        self.invalidation_buffer.pending_count > 0
                        or self.invalidation_buffer.full_refresh_required
                    ),
                    settled_ticket_ids=(),
                    settlement_evidence_ids=(),
                    last_success_at=snapshot.last_success_at,
                )

            source_gap_states = (
                ()
                if source_snapshot.source_gap_state is None
                else (source_snapshot.source_gap_state,)
            )
            source_sync_states = (
                ()
                if source_snapshot.source_sync_state is None
                else (source_snapshot.source_sync_state,)
            )
            delivered = self.desktop_consumer.drain(
                as_of=now,
                view=self.causal_view,
            )
            affected, full_refresh, backlog = self._drain_invalidations()

            newly_registered: list[str] = []
            retired: list[str] = []

            def register(input_id: str, **selectors: object) -> None:
                before = input_id in self.dependency_index.input_ids
                self._register_input(input_id, **selectors)
                if not before:
                    newly_registered.append(input_id)

            def retire(input_id: str) -> None:
                before = input_id in self.dependency_index.input_ids
                self._retire_input(input_id)
                if before:
                    retired.append(input_id)

            registered = self.lifecycle.register_eligible(
                self.market_store,
                as_of=now,
                required_history=self.required_history,
                register_input=register,
                retire_input=retire,
            )
            for input_id in registered:
                if input_id not in newly_registered:
                    newly_registered.append(input_id)

            require_learning_handoff()
            authority_resolutions = self._settlement_resolutions(
                as_of=now,
                _captured_outcome_authority=outcome_authority,
                _captured_outcome_resolve=outcome_resolve,
                _captured_outcome_resolve_witness=outcome_resolve_witness,
            )
            require_learning_handoff()
            resolutions = _snapshot_fn(
                authority_resolutions,
                as_of=now,
            )
            self._state.validate_settlement_evidence(
                settlement_evidence=resolutions
            )
            handoff_resolutions: tuple[SettlementResolution, ...] | None = None
            if learning_handoff is not None:
                handoff_resolutions = _snapshot_fn(
                    resolutions,
                    as_of=now,
                )
                if learning_prepare is not None:
                    learning_prepare(
                        paper_book_path=self.paper_book_path,
                        resolutions=handoff_resolutions,
                        at=now,
                    )
                    require_learning_handoff()
                    for resolution in handoff_resolutions:
                        resolution.validate(as_of=resolution.available_at)
            require_learning_handoff()
            settled, evidence_ids = self._settle(resolutions=resolutions)
            require_learning_handoff()
            if learning_handoff is not None:
                assert handoff_resolutions is not None
                assert learning_reconcile is not None
                learning_reconcile(
                    paper_book_path=self.paper_book_path,
                    resolutions=handoff_resolutions,
                    settled_ticket_ids=settled,
                    at=now,
                )
                require_learning_handoff()

            cycle_index = self._state.operational_snapshot().cycles_completed + 1
            self._state.record_success(
                at=now,
                full_refresh=full_refresh,
                settlement_evidence=resolutions,
            )
            return ContinuousTickResult(
                session_id=self.session_id,
                cycle_index=cycle_index,
                source_id=cycle.source_id,
                source_provider_unavailable=False,
                source_gap_states=source_gap_states,
                source_sync_states=source_sync_states,
                committed_delta_ids=cycle.committed_delta_ids,
                delivered_delta_ids=delivered,
                affected_input_ids=affected,
                registered_input_ids=tuple(newly_registered),
                retired_input_ids=tuple(retired),
                full_refresh_required=full_refresh,
                invalidation_backlog=backlog,
                settled_ticket_ids=settled,
                settlement_evidence_ids=evidence_ids,
                last_success_at=self._state.operational_snapshot().last_success_at or now,
            )
        except Exception as exc:
            self._state.record_failure(code=type(exc).__name__)
            raise

# Seal the consumer entry after class creation. The metaclass data descriptor also
# makes direct type.__setattr__/type.__delattr__ respect the same class-level fence.
_ContinuousSessionCoordinatorMeta._settle = _build_settlement_consumer_class_guard(
    "_settle"
)
ContinuousSessionCoordinator._settlement_consumer_bindings_sealed = True
