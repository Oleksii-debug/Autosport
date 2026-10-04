from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
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
from .integrity import atomic_write_json
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


def _bind_canonical_settlement_engine(method):
    """Inject settlement constructor/scope authority through closure-owned seams."""

    canonical_engine_type = SettlementEngine
    canonical_scope_impl = _canonical_open_quote_keys_for_book
    canonical_lifecycle_type = ContinuousEventLifecycle
    canonical_lifecycle_get = ContinuousEventLifecycle.get
    canonical_lifecycle_read = ContinuousEventLifecycle._read
    canonical_lifecycle_record_type = EventLifecycleRecord
    canonical_resolution_type = SettlementResolution
    canonical_datetime_type = datetime
    canonical_timezone_utc = timezone.utc
    canonical_book_type = PaperBook
    canonical_book_load = PaperBook.load
    canonical_book_save = PaperBook.save
    canonical_workspace_lock_type = WorkspaceEconomicLock
    canonical_collector_source_id_get = HeadlessCollectorService.source_id.fget
    canonical_hex = frozenset("0123456789abcdef")
    canonical_outcomes = frozenset({"win", "loss", "void"})

    def canonical_text(value: object, field: str) -> str:
        if type(value) is not str or not value or value.strip() != value:
            raise ValueError(f"{field} must be a non-empty trimmed string")
        return value

    def canonical_instant(value: object, field: str) -> datetime:
        raw = canonical_text(value, field)
        try:
            parsed = canonical_datetime_type.fromisoformat(
                raw.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(f"{field} must be valid ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware ISO-8601")
        return parsed.astimezone(canonical_timezone_utc)

    def canonical_resolution_validate(
        resolution: SettlementResolution,
        *,
        as_of: str,
    ) -> None:
        if type(resolution) is not canonical_resolution_type:
            raise TypeError("settlement resolution must be canonical")
        canonical_text(resolution.event_identity, "event_identity")
        canonical_text(resolution.settlement_ref, "settlement_ref")
        canonical_text(resolution.evidence_id, "evidence_id")
        digest = resolution.evidence_sha256
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in canonical_hex for character in digest)
        ):
            raise ValueError(
                "evidence_sha256 must be a lowercase SHA-256 hex digest"
            )
        cutoff = canonical_instant(as_of, "as_of")
        available = canonical_instant(resolution.available_at, "available_at")
        if available > cutoff:
            raise ValueError(
                "settlement evidence is not causally available at session cutoff"
            )
        quote_outcomes = resolution.quote_outcomes
        if type(quote_outcomes) is not dict or not quote_outcomes:
            raise ValueError("quote_outcomes must be a non-empty exact dict")
        for quote_key, outcome in quote_outcomes.items():
            canonical_text(quote_key, "quote_outcomes quote_key")
            if type(outcome) is not str or outcome not in canonical_outcomes:
                raise ValueError("quote_outcomes contains unsupported outcome")

    def canonical_scope_resolver(
        coordinator,
        book: PaperBook,
        event_identity: str,
        settlement_ref: str,
        settled_at: str,
    ) -> set[str]:
        live_source_id = canonical_collector_source_id_get(coordinator.collector)
        if live_source_id != coordinator._settlement_source_id:
            raise ContinuousSessionError(
                "collector source identity changed after settlement authority binding"
            )
        lifecycle = coordinator.lifecycle
        if type(lifecycle) is not canonical_lifecycle_type:
            raise ContinuousSessionError(
                "settlement lifecycle authority must be canonical"
            )
        if (
            canonical_lifecycle_type.get is not canonical_lifecycle_get
            or canonical_lifecycle_type._read is not canonical_lifecycle_read
            or "get" in lifecycle.__dict__
            or "_read" in lifecycle.__dict__
        ):
            raise ContinuousSessionError(
                "settlement lifecycle lookup dispatch changed"
            )
        return canonical_scope_impl(
            coordinator,
            book,
            event_identity,
            settlement_ref,
            settled_at,
            _lifecycle_get=canonical_lifecycle_get,
            _lifecycle_record_type=canonical_lifecycle_record_type,
            _settlement_instant=canonical_instant,
        )

    def guarded(self, *args, **kwargs):
        protected = {
            "_settlement_engine_type": "settlement engine origin is internal product authority",
            "_settlement_scope_resolver": "settlement scope origin is internal product authority",
            "_settlement_resolution_type": "settlement evidence type is internal product authority",
            "_settlement_resolution_validate": "settlement evidence validator is internal product authority",
            "_settlement_instant": "settlement clock parser is internal product authority",
            "_paper_book_type": "settlement book type is internal product authority",
            "_paper_book_load": "settlement book loader is internal product authority",
            "_paper_book_save": "settlement book saver is internal product authority",
            "_workspace_lock_type": "settlement lock origin is internal product authority",
        }
        for name, message in protected.items():
            if name in kwargs:
                raise TypeError(message)
        kwargs["_settlement_engine_type"] = canonical_engine_type
        kwargs["_settlement_scope_resolver"] = canonical_scope_resolver
        kwargs["_settlement_resolution_type"] = canonical_resolution_type
        kwargs["_settlement_resolution_validate"] = canonical_resolution_validate
        kwargs["_settlement_instant"] = canonical_instant
        kwargs["_paper_book_type"] = canonical_book_type
        kwargs["_paper_book_load"] = canonical_book_load
        kwargs["_paper_book_save"] = canonical_book_save
        kwargs["_workspace_lock_type"] = canonical_workspace_lock_type
        return method(self, *args, **kwargs)

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
        protected = {
            "_settle",
            "_settlement_resolutions",
            "_recovered_settlement_resolutions",
            "__setattr__",
            "__delattr__",
            "_settlement_consumer_bindings_sealed",
        }
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
            "_settlement_resolutions",
            "_recovered_settlement_resolutions",
            "__setattr__",
            "__delattr__",
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
            "_settlement_resolutions",
            "_recovered_settlement_resolutions",
            "__setattr__",
            "__delattr__",
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
        if type(self.quote_outcomes) is not dict or not self.quote_outcomes:
            raise ValueError("quote_outcomes must be a non-empty exact dict")
        for quote_key, outcome in self.quote_outcomes.items():
            _text(quote_key, "quote_outcomes quote_key")
            if type(outcome) is not str or outcome not in {"win", "loss", "void"}:
                raise ValueError("quote_outcomes contains unsupported outcome")


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

    def prepared_settlement_resolutions(
        self,
        *,
        paper_book_path: Path,
    ) -> tuple[SettlementResolution, ...]:
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
    settlement_evidence: tuple[dict[str, str], ...]
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


def _settlement_outcomes_sha256(evidence: SettlementResolution) -> str:
    payload = json.dumps(
        dict(sorted(evidence.quote_outcomes.items())),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bind_continuous_state_settlement_integrity(method):
    """Bind durable outcome interpretation to module-load integrity roots."""

    canonical_json_dumps = json.dumps
    canonical_sha256 = hashlib.sha256
    canonical_datetime_type = datetime
    canonical_timezone_utc = timezone.utc

    def canonical_instant(value: object, field: str) -> datetime:
        if type(value) is not str or not value or value.strip() != value:
            raise ValueError(f"{field} must be a non-empty trimmed string")
        try:
            parsed = canonical_datetime_type.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError(f"{field} must be valid ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware ISO-8601")
        return parsed.astimezone(canonical_timezone_utc)

    def outcomes_digest(evidence: SettlementResolution) -> str:
        payload = canonical_json_dumps(
            dict(sorted(evidence.quote_outcomes.items())),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return canonical_sha256(payload).hexdigest()

    def guarded(self, raw, settlement_evidence):
        return method(
            self,
            raw,
            settlement_evidence,
            _settlement_instant=canonical_instant,
            _settlement_outcomes_digest=outcomes_digest,
        )

    guarded.__name__ = method.__name__
    guarded.__qualname__ = method.__qualname__
    guarded.__doc__ = method.__doc__
    guarded.__annotations__ = method.__annotations__
    return guarded


class _ContinuousSessionState:
    _SCHEMA = "autosport.continuous_session"
    _VERSION = 4
    _V2_FIELDS = frozenset(
        {
            "schema",
            "schema_version",
            "session_id",
            "source_id",
            "state",
            "started_at",
            "cycles_completed",
            "last_success_at",
            "last_error_code",
            "last_full_refresh_at",
            "settlement_evidence",
            "source_gap_state",
            "source_sync_state",
            "source_state_delta_id",
            "source_unresolved_gap_delta_ids",
            "source_projection_stream_epoch",
            "source_state_projection_backlog",
        }
    )
    _V3_FIELDS = frozenset(
        {
            *_V2_FIELDS,
            "settlement_outcome_digests",
        }
    )
    _FIELDS = frozenset(
        {
            *_V3_FIELDS,
            "pending_settlement_resolutions",
        }
    )

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
        self.source_id = _text(source_id, "source_id")
        self._clock = clock

        # Session identity and economic settlement evidence share the workspace's
        # single-writer authority. Serialize first creation/read so two processes
        # cannot independently mint competing session identities from the same
        # pristine workspace.
        with WorkspaceEconomicLock(self.path.parent):
            if self.path.exists():
                raw = self._read()
                existing_source = raw["source_id"]
                if existing_source != self.source_id:
                    raise ContinuousSessionError(
                        "durable session source_id does not match configured source"
                    )
                if session_id is not None and raw["session_id"] != _text(
                    session_id, "session_id"
                ):
                    raise ContinuousSessionError(
                        "durable session_id does not match configured session"
                    )
            else:
                resolved_id = _text(
                    session_id or str(uuid.uuid4()),
                    "session_id",
                )
                started_at = clock()
                _instant(started_at, "started_at")
                atomic_write_json(
                    self.path,
                    {
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
                        "settlement_evidence": [],
                        "settlement_outcome_digests": {},
                        "pending_settlement_resolutions": [],
                        "source_gap_state": None,
                        "source_sync_state": None,
                        "source_state_delta_id": None,
                        "source_unresolved_gap_delta_ids": [],
                        "source_projection_stream_epoch": None,
                        "source_state_projection_backlog": False,
                    },
                )
                self._read()

    @staticmethod
    def _validate_settlement_evidence(raw: object) -> tuple[dict[str, str], ...]:
        if type(raw) is not list:
            raise ContinuousSessionError("settlement_evidence must be a list")
        values: list[dict[str, str]] = []
        for item in raw:
            if type(item) is not dict:
                raise ContinuousSessionError(
                    "settlement_evidence entries must be objects"
                )
            if set(item) != {
                "event_identity",
                "settlement_ref",
                "evidence_id",
                "evidence_sha256",
                "available_at",
            }:
                raise ContinuousSessionError(
                    "settlement_evidence entry fields mismatch"
                )
            _text(item["event_identity"], "settlement_evidence event_identity")
            _text(item["settlement_ref"], "settlement_evidence settlement_ref")
            _text(item["evidence_id"], "settlement_evidence evidence_id")
            _sha256(item["evidence_sha256"], "settlement_evidence evidence_sha256")
            _instant(item["available_at"], "settlement_evidence available_at")
            normalized = {
                "event_identity": item["event_identity"],
                "settlement_ref": item["settlement_ref"],
                "evidence_id": item["evidence_id"],
                "evidence_sha256": item["evidence_sha256"],
                "available_at": item["available_at"],
            }
            if any(
                prior["evidence_id"] == normalized["evidence_id"]
                and prior != normalized
                for prior in values
            ):
                raise ContinuousSessionError(
                    "durable settlement evidence id is conflicting"
                )
            if any(
                (
                    prior["event_identity"],
                    prior["settlement_ref"],
                )
                == (
                    normalized["event_identity"],
                    normalized["settlement_ref"],
                )
                and prior != normalized
                for prior in values
            ):
                raise ContinuousSessionError(
                    "durable settlement event/reference evidence is conflicting"
                )
            if normalized not in values:
                values.append(normalized)
        return tuple(values)

    @staticmethod
    def _validate_pending_settlement_resolutions(
        raw: object,
    ) -> tuple[dict[str, object], ...]:
        if type(raw) is not list:
            raise ContinuousSessionError(
                "pending_settlement_resolutions must be a list"
            )
        values: list[dict[str, object]] = []
        for item in raw:
            if type(item) is not dict or set(item) != {
                "event_identity",
                "settlement_ref",
                "quote_outcomes",
                "evidence_id",
                "evidence_sha256",
                "available_at",
            }:
                raise ContinuousSessionError(
                    "pending settlement resolution fields mismatch"
                )
            _text(item["event_identity"], "pending settlement event_identity")
            _text(item["settlement_ref"], "pending settlement settlement_ref")
            _text(item["evidence_id"], "pending settlement evidence_id")
            _sha256(item["evidence_sha256"], "pending settlement evidence_sha256")
            _instant(item["available_at"], "pending settlement available_at")
            quote_outcomes = item["quote_outcomes"]
            if type(quote_outcomes) is not dict or not quote_outcomes:
                raise ContinuousSessionError(
                    "pending settlement quote_outcomes must be a non-empty exact dict"
                )
            normalized_outcomes: dict[str, str] = {}
            for quote_key, outcome in quote_outcomes.items():
                canonical_key = _text(
                    quote_key,
                    "pending settlement quote_outcomes quote_key",
                )
                if type(outcome) is not str or outcome not in {
                    "win",
                    "loss",
                    "void",
                }:
                    raise ContinuousSessionError(
                        "pending settlement quote_outcomes contains unsupported outcome"
                    )
                normalized_outcomes[canonical_key] = outcome
            normalized = {
                "event_identity": item["event_identity"],
                "settlement_ref": item["settlement_ref"],
                "quote_outcomes": dict(sorted(normalized_outcomes.items())),
                "evidence_id": item["evidence_id"],
                "evidence_sha256": item["evidence_sha256"],
                "available_at": item["available_at"],
            }
            if any(
                prior["evidence_id"] == normalized["evidence_id"]
                and prior != normalized
                for prior in values
            ):
                raise ContinuousSessionError(
                    "pending settlement evidence id is conflicting"
                )
            if any(
                (
                    prior["event_identity"],
                    prior["settlement_ref"],
                )
                == (
                    normalized["event_identity"],
                    normalized["settlement_ref"],
                )
                and prior != normalized
                for prior in values
            ):
                raise ContinuousSessionError(
                    "pending settlement event/reference is conflicting"
                )
            if normalized not in values:
                values.append(normalized)
        return tuple(values)

    def _read(self) -> dict[str, Any]:
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ContinuousSessionError(
                "cannot verify continuous session state"
            ) from exc
        if type(raw) is not dict or raw.get("schema") != self._SCHEMA:
            raise ContinuousSessionError("continuous session state schema/identity mismatch")
        version = raw.get("schema_version")
        if version == 2 and set(raw) == self._V2_FIELDS:
            legacy_v2 = True
            legacy_v3 = False
        elif version == 3 and set(raw) == self._V3_FIELDS:
            legacy_v2 = False
            legacy_v3 = True
        elif version == self._VERSION and set(raw) == self._FIELDS:
            legacy_v2 = False
            legacy_v3 = False
        else:
            raise ContinuousSessionError("continuous session state schema/identity mismatch")
        if raw["source_id"] != self.source_id:
            raise ContinuousSessionError("continuous session state schema/identity mismatch")
        _text(raw["session_id"], "session_id")
        _instant(raw["started_at"], "started_at")
        try:
            state = SessionState(raw["state"])
        except ValueError as exc:
            raise ContinuousSessionError("unsupported continuous session state") from exc
        cycles = raw["cycles_completed"]
        if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
            raise ContinuousSessionError("cycles_completed must be a non-negative integer")
        for name in ("last_success_at", "last_full_refresh_at"):
            if raw[name] is not None:
                _instant(raw[name], name)
        if raw["last_error_code"] is not None:
            _text(raw["last_error_code"], "last_error_code")
        evidence = self._validate_settlement_evidence(raw["settlement_evidence"])
        if legacy_v2:
            outcome_digests: dict[str, str | None] = {
                item["evidence_id"]: None for item in evidence
            }
            raw["schema_version"] = self._VERSION
            raw["settlement_outcome_digests"] = outcome_digests
            raw["pending_settlement_resolutions"] = []
        else:
            outcome_digests = raw["settlement_outcome_digests"]
            if (
                type(outcome_digests) is not dict
                or set(outcome_digests)
                != {item["evidence_id"] for item in evidence}
            ):
                raise ContinuousSessionError(
                    "settlement outcome digest index does not match durable evidence"
                )
            for evidence_id, digest in outcome_digests.items():
                _text(evidence_id, "settlement outcome evidence_id")
                if digest is not None:
                    _sha256(digest, "settlement outcome digest")
            if legacy_v3:
                raw["schema_version"] = self._VERSION
                raw["pending_settlement_resolutions"] = []

        pending = self._validate_pending_settlement_resolutions(
            raw["pending_settlement_resolutions"]
        )
        evidence_by_id = {
            item["evidence_id"]: item
            for item in evidence
        }
        for item in pending:
            durable = evidence_by_id.get(item["evidence_id"])
            if durable is None or any(
                durable[field] != item[field]
                for field in (
                    "event_identity",
                    "settlement_ref",
                    "evidence_id",
                    "evidence_sha256",
                    "available_at",
                )
            ):
                raise ContinuousSessionError(
                    "pending settlement resolution is not bound to durable evidence"
                )
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
            _text(raw["source_projection_stream_epoch"], "source_projection_stream_epoch")
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
        raw["settlement_evidence"] = [dict(item) for item in evidence]
        raw["pending_settlement_resolutions"] = [
            {
                **item,
                "quote_outcomes": dict(item["quote_outcomes"]),
            }
            for item in pending
        ]
        return raw

    def snapshot(self) -> ContinuousSessionStatus:
        raw = self._read()
        return ContinuousSessionStatus(
            session_id=raw["session_id"],
            source_id=raw["source_id"],
            state=SessionState(raw["state"]),
            cycles_completed=raw["cycles_completed"],
            last_success_at=raw["last_success_at"],
            last_error_code=raw["last_error_code"],
            last_full_refresh_at=raw["last_full_refresh_at"],
            settlement_evidence=tuple(raw["settlement_evidence"]),
            source_gap_state=raw["source_gap_state"],
            source_sync_state=raw["source_sync_state"],
            source_state_delta_id=raw["source_state_delta_id"],
            source_unresolved_gap_delta_ids=tuple(
                raw["source_unresolved_gap_delta_ids"]
            ),
            source_projection_stream_epoch=raw["source_projection_stream_epoch"],
            source_state_projection_backlog=raw[
                "source_state_projection_backlog"
            ],
        )

    @property
    def session_id(self) -> str:
        return self._read()["session_id"]

    def _update(self, mutate: Callable[[dict[str, Any]], None]) -> None:
        # Keep read/validate/mutate/publish under the same cross-process economic
        # writer authority. atomic_write_json makes replacement durable, but by
        # itself cannot prevent two processes from both deriving from one stale
        # pre-state and last-writer-wins erasing settlement evidence.
        with WorkspaceEconomicLock(self.path.parent):
            raw = self._read()
            mutate(raw)
            atomic_write_json(self.path, raw)
            self._read()

    def set_state(self, state: SessionState, *, reason: str | None = None) -> None:
        if not isinstance(state, SessionState):
            raise TypeError("state must be SessionState")

        def mutate(raw: dict[str, Any]) -> None:
            raw["state"] = state.value
            if reason is not None:
                raw["last_error_code"] = _text(reason, "reason")

        self._update(mutate)

    @staticmethod
    def _normalized_settlement_evidence(
        evidence: SettlementResolution,
    ) -> dict[str, str]:
        return {
            "event_identity": evidence.event_identity,
            "settlement_ref": evidence.settlement_ref,
            "evidence_id": evidence.evidence_id,
            "evidence_sha256": evidence.evidence_sha256,
            "available_at": _instant(
                evidence.available_at,
                "available_at",
            ).isoformat(),
        }

    @_bind_continuous_state_settlement_integrity
    def _merge_settlement_evidence(
        self,
        raw: dict[str, Any],
        settlement_evidence: tuple[SettlementResolution, ...],
        *,
        _settlement_instant: Callable[[object, str], datetime],
        _settlement_outcomes_digest: Callable[[SettlementResolution], str],
    ) -> tuple[
        list[dict[str, str]],
        dict[str, str | None],
        list[dict[str, object]],
    ]:
        known = {
            item["evidence_id"]: item
            for item in raw["settlement_evidence"]
        }
        known_pairs = {
            (item["event_identity"], item["settlement_ref"]): item
            for item in raw["settlement_evidence"]
        }
        outcome_digests = dict(raw["settlement_outcome_digests"])
        pending = {
            item["evidence_id"]: {
                **item,
                "quote_outcomes": dict(item["quote_outcomes"]),
            }
            for item in raw["pending_settlement_resolutions"]
        }
        pending_pairs = {
            (item["event_identity"], item["settlement_ref"]): item
            for item in pending.values()
        }
        for evidence in settlement_evidence:
            normalized = {
                "event_identity": evidence.event_identity,
                "settlement_ref": evidence.settlement_ref,
                "evidence_id": evidence.evidence_id,
                "evidence_sha256": evidence.evidence_sha256,
                "available_at": _settlement_instant(
                    evidence.available_at,
                    "available_at",
                ).isoformat(),
            }
            outcomes_digest = _settlement_outcomes_digest(evidence)
            existing = known.get(evidence.evidence_id)
            if existing is not None and existing != normalized:
                raise ContinuousSessionError(
                    "settlement evidence id conflicts with durable evidence"
                )
            pair = (evidence.event_identity, evidence.settlement_ref)
            pair_existing = known_pairs.get(pair)
            if pair_existing is not None and pair_existing != normalized:
                raise ContinuousSessionError(
                    "settlement event/reference conflicts with durable evidence"
                )
            previous_digest = outcome_digests.get(evidence.evidence_id)
            if existing is not None and previous_digest is None:
                raise ContinuousSessionError(
                    "legacy settlement evidence lacks durable outcome interpretation"
                )
            if previous_digest is not None and previous_digest != outcomes_digest:
                raise ContinuousSessionError(
                    "settlement outcome interpretation conflicts with durable evidence"
                )
            pending_payload = {
                **normalized,
                "quote_outcomes": dict(sorted(evidence.quote_outcomes.items())),
            }
            existing_pending = pending.get(evidence.evidence_id)
            if existing_pending is not None and existing_pending != pending_payload:
                raise ContinuousSessionError(
                    "pending settlement evidence id conflicts with durable resolution"
                )
            pending_pair_existing = pending_pairs.get(pair)
            if (
                pending_pair_existing is not None
                and pending_pair_existing != pending_payload
            ):
                raise ContinuousSessionError(
                    "pending settlement event/reference conflicts with durable resolution"
                )
            known[evidence.evidence_id] = normalized
            known_pairs[pair] = normalized
            outcome_digests[evidence.evidence_id] = outcomes_digest
            pending[evidence.evidence_id] = pending_payload
            pending_pairs[pair] = pending_payload
        return (
            list(sorted(known.values(), key=lambda item: item["evidence_id"])),
            dict(sorted(outcome_digests.items())),
            list(sorted(pending.values(), key=lambda item: item["evidence_id"])),
        )

    def validate_settlement_evidence(
        self,
        *,
        settlement_evidence: tuple[SettlementResolution, ...],
    ) -> None:
        self._merge_settlement_evidence(self._read(), settlement_evidence)

    def pending_settlement_resolutions(
        self,
    ) -> tuple[SettlementResolution, ...]:
        raw = self._read()
        pending = tuple(
            SettlementResolution(
                event_identity=item["event_identity"],
                settlement_ref=item["settlement_ref"],
                quote_outcomes=dict(item["quote_outcomes"]),
                evidence_id=item["evidence_id"],
                evidence_sha256=item["evidence_sha256"],
                available_at=item["available_at"],
            )
            for item in raw["pending_settlement_resolutions"]
        )
        # Re-prove exact metadata/outcome-digest authority through the closure-bound
        # settlement integrity root before exposing pending truth for recovery.
        self._merge_settlement_evidence(raw, pending)
        return pending

    def record_settlement_evidence(
        self,
        *,
        settlement_evidence: tuple[SettlementResolution, ...],
    ) -> None:
        """Durably bind settlement interpretation before any PAPER economic commit."""

        def mutate(raw: dict[str, Any]) -> None:
            evidence, outcome_digests, pending = self._merge_settlement_evidence(
                raw,
                settlement_evidence,
            )
            raw["settlement_evidence"] = evidence
            raw["settlement_outcome_digests"] = outcome_digests
            raw["pending_settlement_resolutions"] = pending

        self._update(mutate)

    def validate_recovered_settlement_evidence(
        self,
        *,
        settlement_evidence: tuple[SettlementResolution, ...],
    ) -> None:
        """Prove recovery is replaying pre-P&L durable truth, never minting new truth."""

        raw = self._read()
        known_ids = {
            item["evidence_id"]
            for item in raw["settlement_evidence"]
        }
        # Reuse the closure-bound durable integrity path for exact evidence,
        # event/reference and quote-outcome digest comparison.  It is intentionally
        # side-effect free here because raw is only the freshly read candidate.
        self._merge_settlement_evidence(raw, settlement_evidence)
        for evidence in settlement_evidence:
            if evidence.evidence_id not in known_ids:
                raise ContinuousSessionError(
                    "recovered settlement evidence was not durably staged before P&L"
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

        self._update(mutate)

    def record_success(
        self,
        *,
        at: str,
        full_refresh: bool,
        settlement_evidence: tuple[SettlementResolution, ...],
    ) -> None:
        timestamp = _instant(at, "at")

        def mutate(raw: dict[str, Any]) -> None:
            raw["cycles_completed"] = int(raw["cycles_completed"]) + 1
            raw["last_success_at"] = timestamp.isoformat()
            raw["last_error_code"] = None
            if full_refresh:
                raw["last_full_refresh_at"] = timestamp.isoformat()

            evidence, outcome_digests, pending = self._merge_settlement_evidence(
                raw,
                settlement_evidence,
            )
            completed_ids = {
                evidence.evidence_id
                for evidence in settlement_evidence
            }
            raw["settlement_evidence"] = evidence
            raw["settlement_outcome_digests"] = outcome_digests
            raw["pending_settlement_resolutions"] = [
                item
                for item in pending
                if item["evidence_id"] not in completed_ids
            ]

        self._update(mutate)

    def record_failure(self, *, code: str) -> None:
        code = _text(code, "code")
        self._update(lambda raw: raw.__setitem__("last_error_code", code))


def _bind_canonical_settlement_resolution_collection(method):
    """Seal externally-authoritative settlement evidence before durable staging."""

    resolution_type = SettlementResolution
    lifecycle_type = ContinuousEventLifecycle
    lifecycle_records = ContinuousEventLifecycle.records
    lifecycle_read = ContinuousEventLifecycle._read
    lifecycle_record_type = EventLifecycleRecord
    collector_source_id_get = HeadlessCollectorService.source_id.fget
    datetime_type = datetime
    timezone_utc = timezone.utc
    valid_hex = frozenset("0123456789abcdef")
    valid_outcomes = frozenset({"win", "loss", "void"})

    def canonical_text(value: object, field: str) -> str:
        if type(value) is not str or not value or value.strip() != value:
            raise ValueError(f"{field} must be a non-empty trimmed string")
        return value

    def canonical_instant(value: object, field: str) -> datetime:
        raw = canonical_text(value, field)
        try:
            parsed = datetime_type.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be valid ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware ISO-8601")
        return parsed.astimezone(timezone_utc)

    def validate_resolution(
        resolution: SettlementResolution,
        *,
        as_of: str,
    ) -> None:
        if type(resolution) is not resolution_type:
            raise TypeError("settlement resolution must be canonical")
        canonical_text(resolution.event_identity, "event_identity")
        canonical_text(resolution.settlement_ref, "settlement_ref")
        canonical_text(resolution.evidence_id, "evidence_id")
        digest = resolution.evidence_sha256
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in valid_hex for character in digest)
        ):
            raise ValueError(
                "evidence_sha256 must be a lowercase SHA-256 hex digest"
            )
        cutoff = canonical_instant(as_of, "as_of")
        available = canonical_instant(resolution.available_at, "available_at")
        if available > cutoff:
            raise ValueError(
                "settlement evidence is not causally available at session cutoff"
            )
        quote_outcomes = resolution.quote_outcomes
        if type(quote_outcomes) is not dict or not quote_outcomes:
            raise ValueError("quote_outcomes must be a non-empty exact dict")
        for quote_key, outcome in quote_outcomes.items():
            canonical_text(quote_key, "quote_outcomes quote_key")
            if type(outcome) is not str or outcome not in valid_outcomes:
                raise ValueError("quote_outcomes contains unsupported outcome")

    def canonical_records(lifecycle) -> tuple[EventLifecycleRecord, ...]:
        if type(lifecycle) is not lifecycle_type:
            raise ContinuousSessionError(
                "settlement lifecycle authority must be canonical"
            )
        if (
            lifecycle_type.records is not lifecycle_records
            or lifecycle_type._read is not lifecycle_read
            or "records" in lifecycle.__dict__
            or "_read" in lifecycle.__dict__
        ):
            raise ContinuousSessionError(
                "settlement lifecycle record dispatch changed"
            )
        records = lifecycle_records(lifecycle)
        if type(records) is not tuple or any(
            type(record) is not lifecycle_record_type for record in records
        ):
            raise ContinuousSessionError(
                "settlement lifecycle returned non-canonical records"
            )
        return records

    def guarded(self, *, as_of: str):
        return method(
            self,
            as_of=as_of,
            _settlement_resolution_type=resolution_type,
            _settlement_resolution_validate=validate_resolution,
            _lifecycle_records=canonical_records,
            _collector_source_id_get=collector_source_id_get,
            _settlement_instant=canonical_instant,
        )

    guarded.__name__ = method.__name__
    guarded.__qualname__ = method.__qualname__
    guarded.__doc__ = method.__doc__
    guarded.__annotations__ = method.__annotations__
    return guarded


def _canonical_open_quote_keys_for_book(
    coordinator,
    book: PaperBook,
    event_identity: str,
    settlement_ref: str,
    settled_at: str,
    *,
    _lifecycle_get: Callable[[ContinuousEventLifecycle, str], EventLifecycleRecord | None],
    _lifecycle_record_type: type[EventLifecycleRecord],
    _settlement_instant: Callable[[object, str], datetime],
) -> set[str]:
    """Resolve the exact provider/sport/native-event scope allowed to mutate P&L."""

    record = _lifecycle_get(coordinator.lifecycle, event_identity)
    if record is not None and type(record) is not _lifecycle_record_type:
        raise ContinuousSessionError(
            "settlement lifecycle lookup returned non-canonical record"
        )
    if record is None:
        raise ContinuousSessionError(
            "settlement event identity is absent from durable lifecycle"
        )
    if record.phase is not EventPhase.COMPLETED:
        raise ContinuousSessionError(
            "settlement event is not durably completed"
        )
    if record.settlement_ref != settlement_ref:
        raise ContinuousSessionError(
            "settlement evidence reference differs from durable lifecycle"
        )
    if record.settlement_discovered_at is None:
        raise ContinuousSessionError(
            "settlement lifecycle lacks durable discovery chronology"
        )
    if _settlement_instant(
        record.settlement_discovered_at,
        "settlement_discovered_at",
    ) > _settlement_instant(settled_at, "settled_at"):
        raise ContinuousSessionError(
            "settlement lifecycle was discovered after the economic cutoff"
        )
    source_id = coordinator._settlement_source_id
    if record.source_id != source_id:
        raise ContinuousSessionError(
            "settlement event source is outside continuous session authority"
        )
    return {
        leg.quote_key
        for ticket in book.tickets.values()
        if ticket.status.value == "open"
        # Settlement is an economic mutation, not merely a compatibility read.
        # Legacy tickets without provider provenance remain loadable, but they
        # cannot safely consume provider-scoped outcome truth. A multi-provider
        # ticket is likewise ambiguous because PaperTicket provenance is
        # ticket-level rather than leg-level; fail closed until every leg can be
        # bound to one provider explicitly.
        if ticket.provider_source_ids == (source_id,)
        for leg in ticket.legs
        # Canonical product tickets store the provider-native event id and sport
        # on every leg. Historical full-identity aliases and sport-less legs
        # remain readable, but cannot authorize P&L because either shape can
        # collide across provider/sport namespaces.
        if leg.event_id == record.event_id
        if leg.sport == record.sport
    }


class ContinuousSessionCoordinator(metaclass=_ContinuousSessionCoordinatorMeta):
    """Compose existing collector/lifecycle/mirror/settlement authorities into one durable loop.

    The coordinator owns only session identity/checkpoint and sequencing. It never becomes
    a market store, delta store, lifecycle store, scheduler, outcome authority, or LLM
    decision engine.
    """

    _settlement_consumer_bindings_sealed = False
    _authority_fields_sealed = False

    def __setattr__(self, name: str, value: object) -> None:
        try:
            sealed = object.__getattribute__(self, "_authority_fields_sealed")
        except AttributeError:
            sealed = False
        if sealed and name in {
            "_authority_fields_sealed",
            "workspace",
            "collector",
            "lifecycle",
            "market_store",
            "desktop_consumer",
            "invalidation_buffer",
            "dependency_index",
            "paper_book_path",
            "outcome_authority",
            "_outcome_resolver",
            "settlement_learning_handoff",
            "_settlement_prepare",
            "_settlement_reconcile",
            "_settlement_prepared_resolutions",
            "_settlement_source_id",
            "clock",
            "required_history",
            "max_invalidation_batches_per_tick",
            "max_invalidation_items_per_batch",
            "causal_view",
            "initial_bankroll",
            "_state",
        }:
            raise ContinuousSessionError(
                f"continuous session authority field {name} is immutable after construction"
            )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        try:
            sealed = object.__getattribute__(self, "_authority_fields_sealed")
        except AttributeError:
            sealed = False
        if sealed and name in {
            "_authority_fields_sealed",
            "workspace",
            "collector",
            "lifecycle",
            "market_store",
            "desktop_consumer",
            "invalidation_buffer",
            "dependency_index",
            "paper_book_path",
            "outcome_authority",
            "_outcome_resolver",
            "settlement_learning_handoff",
            "_settlement_prepare",
            "_settlement_reconcile",
            "_settlement_prepared_resolutions",
            "_settlement_source_id",
            "clock",
            "required_history",
            "max_invalidation_batches_per_tick",
            "max_invalidation_items_per_batch",
            "causal_view",
            "initial_bankroll",
            "_state",
        }:
            raise ContinuousSessionError(
                f"continuous session authority field {name} is immutable after construction"
            )
        object.__delattr__(self, name)

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
        self._authority_fields_sealed = False
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
        outcome_resolver = (
            None
            if outcome_authority is None
            else getattr(outcome_authority, "resolve", None)
        )
        if outcome_resolver is not None and not callable(outcome_resolver):
            raise TypeError("outcome_authority.resolve must be callable")
        if outcome_authority is not None and outcome_resolver is None:
            raise TypeError("outcome_authority.resolve must be callable")

        settlement_prepare = None
        settlement_reconcile = None
        settlement_prepared_resolutions = None
        if settlement_learning_handoff is not None:
            settlement_reconcile = getattr(
                settlement_learning_handoff,
                "reconcile_after_settlement",
                None,
            )
            if not callable(settlement_reconcile):
                raise TypeError(
                    "settlement_learning_handoff.reconcile_after_settlement must be callable"
                )
            settlement_prepare = getattr(
                settlement_learning_handoff,
                "prepare_settlement",
                None,
            )
            if settlement_prepare is not None and not callable(settlement_prepare):
                raise TypeError(
                    "settlement_learning_handoff.prepare_settlement must be callable"
                )
            settlement_prepared_resolutions = getattr(
                settlement_learning_handoff,
                "prepared_settlement_resolutions",
                None,
            )
            if (
                settlement_prepared_resolutions is not None
                and not callable(settlement_prepared_resolutions)
            ):
                raise TypeError(
                    "settlement_learning_handoff.prepared_settlement_resolutions must be callable"
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
        self._outcome_resolver = outcome_resolver
        self.settlement_learning_handoff = settlement_learning_handoff
        self._settlement_prepare = settlement_prepare
        self._settlement_reconcile = settlement_reconcile
        self._settlement_prepared_resolutions = settlement_prepared_resolutions
        self._settlement_source_id = _text(
            collector.source_id,
            "collector source_id",
        )
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
            source_id=self._settlement_source_id,
            clock=self.clock,
        )
        self._authority_fields_sealed = True

    @property
    def session_id(self) -> str:
        return self._state.session_id

    def status(self) -> ContinuousSessionStatus:
        snapshot = self._state.snapshot()
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

    def pause(self) -> None:
        self._state.set_state(SessionState.PAUSED)

    def stop(self, reason: str = "operator_stop") -> None:
        self._state.set_state(SessionState.STOPPED, reason=reason)

    def resume(self) -> None:
        current = self._state.snapshot().state
        if current not in {SessionState.PAUSED, SessionState.STOPPED}:
            return
        self._state.set_state(SessionState.RUNNING)

    def _require_running(self) -> None:
        state = self._state.snapshot().state
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
            batch, routed = self.invalidation_buffer.drain_and_route(
                self.dependency_index,
                max_items=self.max_invalidation_items_per_batch,
            )
            if not isinstance(batch, MirrorInvalidationBatch):
                raise ContinuousSessionError(
                    "invalidation buffer returned an invalid batch"
                )
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
        snapshot = self._state.snapshot()
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
        return self._state.snapshot()

    @_seal_settlement_consumer_entry
    @_bind_canonical_settlement_resolution_collection
    def _settlement_resolutions(
        self,
        *,
        as_of: str,
        _settlement_resolution_type: type[SettlementResolution],
        _settlement_resolution_validate: Callable[..., None],
        _lifecycle_records: Callable[[ContinuousEventLifecycle], tuple[EventLifecycleRecord, ...]],
        _collector_source_id_get: Callable[[HeadlessCollectorService], str],
        _settlement_instant: Callable[[object, str], datetime],
    ) -> tuple[SettlementResolution, ...]:
        if self._outcome_resolver is None:
            return ()
        if _collector_source_id_get(self.collector) != self._settlement_source_id:
            raise ContinuousSessionError(
                "collector source identity changed after settlement authority binding"
            )
        cutoff = _settlement_instant(as_of, "as_of")
        resolutions: list[SettlementResolution] = []
        for record in _lifecycle_records(self.lifecycle):
            # A continuous session is bound to exactly one collector source. A
            # shared lifecycle may contain other providers, but their settlement
            # records are outside this session's economic authority.
            if record.source_id != self._settlement_source_id:
                continue
            if record.phase is not EventPhase.COMPLETED or record.settlement_ref is None:
                continue
            if record.settlement_discovered_at is None:
                raise ContinuousSessionError(
                    "settlement lifecycle lacks durable discovery chronology"
                )
            if _settlement_instant(
                record.settlement_discovered_at,
                "settlement_discovered_at",
            ) > cutoff:
                raise ContinuousSessionError(
                    "settlement lifecycle was discovered after the evidence cutoff"
                )
            resolution = self._outcome_resolver(record, as_of=as_of)
            if resolution is None:
                continue
            if type(resolution) is not _settlement_resolution_type:
                raise ContinuousSessionError(
                    "outcome authority must return canonical SettlementResolution or None"
                )
            try:
                _settlement_resolution_validate(resolution, as_of=as_of)
            except (TypeError, ValueError) as exc:
                raise ContinuousSessionError(
                    "settlement resolution failed canonical validation"
                ) from exc
            if resolution.event_identity != record.identity:
                raise ContinuousSessionError(
                    "settlement evidence event identity does not match lifecycle identity"
                )
            if resolution.settlement_ref != record.settlement_ref:
                raise ContinuousSessionError(
                    "settlement evidence reference does not match lifecycle evidence"
                )
            resolutions.append(resolution)
        return tuple(resolutions)

    @_seal_settlement_consumer_entry
    @_bind_canonical_settlement_resolution_collection
    def _recovered_settlement_resolutions(
        self,
        *,
        as_of: str,
        _settlement_resolution_type: type[SettlementResolution],
        _settlement_resolution_validate: Callable[..., None],
        _lifecycle_records: Callable[[ContinuousEventLifecycle], tuple[EventLifecycleRecord, ...]],
        _collector_source_id_get: Callable[[HeadlessCollectorService], str],
        _settlement_instant: Callable[[object, str], datetime],
    ) -> tuple[SettlementResolution, ...]:
        if self._settlement_prepared_resolutions is None:
            return ()
        recovered = self._settlement_prepared_resolutions(
            paper_book_path=self.paper_book_path,
        )
        if type(recovered) is not tuple:
            raise ContinuousSessionError(
                "prepared settlement recovery must return a tuple"
            )
        for resolution in recovered:
            if type(resolution) is not _settlement_resolution_type:
                raise ContinuousSessionError(
                    "prepared settlement recovery returned non-canonical evidence"
                )
            try:
                _settlement_resolution_validate(resolution, as_of=as_of)
            except (TypeError, ValueError) as exc:
                raise ContinuousSessionError(
                    "prepared settlement recovery failed canonical validation"
                ) from exc
        self._state.validate_recovered_settlement_evidence(
            settlement_evidence=recovered,
        )
        return recovered

    def _load_book(self) -> PaperBook:
        if self.paper_book_path.exists():
            return PaperBook.load(self.paper_book_path)
        return PaperBook(self.initial_bankroll)

    @_seal_settlement_consumer_entry
    @_bind_canonical_settlement_engine
    def _settle(
        self,
        *,
        resolutions: tuple[SettlementResolution, ...],
        settled_at: str,
        _settlement_engine_type: type[SettlementEngine],
        _settlement_scope_resolver: Callable[[object, PaperBook, str], set[str]],
        _settlement_resolution_type: type[SettlementResolution],
        _settlement_resolution_validate: Callable[..., None],
        _settlement_instant: Callable[..., datetime],
        _paper_book_type: type[PaperBook],
        _paper_book_load: Callable[[str | Path], PaperBook],
        _paper_book_save: Callable[[PaperBook, str | Path], None],
        _workspace_lock_type: type[WorkspaceEconomicLock],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if type(resolutions) is not tuple:
            raise TypeError("settlement resolutions must be a tuple")
        if not resolutions:
            return (), ()
        if SettlementEngine is not _settlement_engine_type:
            raise ContinuousSessionError(
                "settlement engine constructor origin changed"
            )

        canonical_settled_at = _settlement_instant(
            settled_at,
            "settled_at",
        ).isoformat()
        unique: dict[str, SettlementResolution] = {}
        event_ref_authority: dict[tuple[str, str], str] = {}
        for resolution in resolutions:
            if type(resolution) is not _settlement_resolution_type:
                raise ContinuousSessionError(
                    "settlement handoff contains non-canonical resolution evidence"
                )
            try:
                _settlement_resolution_validate(
                    resolution,
                    as_of=canonical_settled_at,
                )
            except (TypeError, ValueError) as exc:
                raise ContinuousSessionError(
                    "settlement resolution failed canonical validation"
                ) from exc

            previous = unique.get(resolution.evidence_id)
            if previous is not None and previous != resolution:
                raise ContinuousSessionError(
                    "settlement evidence id has multiple authorities"
                )
            event_ref = (resolution.event_identity, resolution.settlement_ref)
            previous_event_ref_authority = event_ref_authority.get(event_ref)
            if (
                previous_event_ref_authority is not None
                and previous_event_ref_authority != resolution.evidence_id
            ):
                raise ContinuousSessionError(
                    "settlement event/reference has multiple evidence authorities"
                )
            unique[resolution.evidence_id] = resolution
            event_ref_authority[event_ref] = resolution.evidence_id

        with _workspace_lock_type(self.workspace):
            if self.paper_book_path.exists():
                book = _paper_book_load(self.paper_book_path)
            else:
                book = _paper_book_type(self.initial_bankroll)
            if type(book) is not _paper_book_type:
                raise ContinuousSessionError(
                    "settlement book loader returned non-canonical type"
                )
            engine = _settlement_engine_type()
            if type(engine) is not _settlement_engine_type:
                raise ContinuousSessionError(
                    "settlement engine constructor returned non-canonical type"
                )
            for resolution in unique.values():
                allowed = _settlement_scope_resolver(
                    self,
                    book,
                    resolution.event_identity,
                    resolution.settlement_ref,
                    canonical_settled_at,
                )
                scoped = {
                    quote_key: outcome
                    for quote_key, outcome in resolution.quote_outcomes.items()
                    if quote_key in allowed
                }
                if scoped:
                    engine.record(scoped)
            settled = tuple(
                engine.settle_ready(
                    book,
                    settled_at=canonical_settled_at,
                )
            )
            if settled:
                _paper_book_save(book, self.paper_book_path)

        return settled, tuple(unique)

    def tick(self) -> ContinuousTickResult:
        self._require_running()
        now = self.clock()
        _instant(now, "now")
        try:
            cycle = self.collector.run_cycle()
            source_snapshot = self._refresh_source_state_projection()
            if cycle.provider_unavailable:
                self._state.record_failure(code="ProviderUnavailableError")
                snapshot = self._state.snapshot()
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
            # The lifecycle is canonical about eligibility; the index is canonical
            # about dependency routing. Keep both outputs for auditability.
            for input_id in registered:
                if input_id not in newly_registered:
                    newly_registered.append(input_id)

            current_resolutions = self._settlement_resolutions(as_of=now)
            # Settlement identity and quote-outcome interpretation are part of the
            # economic commit protocol. Persist newly observed truth before any learner
            # side effect or PaperBook mutation. A later recovery handoff may replay
            # only evidence already proven by this durable pre-P&L checkpoint.
            self._state.record_settlement_evidence(
                settlement_evidence=current_resolutions
            )
            if self._settlement_prepare is not None:
                self._settlement_prepare(
                    paper_book_path=self.paper_book_path,
                    resolutions=current_resolutions,
                    at=now,
                )
            recovered_resolutions = self._recovered_settlement_resolutions(
                as_of=now,
            )
            resolutions = current_resolutions + recovered_resolutions
            settled, evidence_ids = self._settle(
                resolutions=resolutions,
                settled_at=now,
            )
            if self._settlement_reconcile is not None:
                self._settlement_reconcile(
                    paper_book_path=self.paper_book_path,
                    resolutions=resolutions,
                    settled_ticket_ids=settled,
                    at=now,
                )

            cycle_index = self._state.snapshot().cycles_completed + 1
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
                last_success_at=self._state.snapshot().last_success_at or now,
            )
        except Exception as exc:
            try:
                self._state.record_failure(code=type(exc).__name__)
            except Exception as checkpoint_exc:
                # Diagnostic checkpoint failure must never replace the market,
                # settlement, replay or economic error that caused this tick to fail.
                try:
                    exc.add_note(
                        "continuous-session failure checkpoint also failed: "
                        f"{type(checkpoint_exc).__name__}: {checkpoint_exc}"
                    )
                except Exception:
                    pass
            raise

# Seal the consumer entry after class creation. The metaclass data descriptor also
# makes direct type.__setattr__/type.__delattr__ respect the same class-level fence.
_ContinuousSessionCoordinatorMeta._settle = _build_settlement_consumer_class_guard(
    "_settle"
)
_ContinuousSessionCoordinatorMeta._settlement_resolutions = (
    _build_settlement_consumer_class_guard("_settlement_resolutions")
)
_ContinuousSessionCoordinatorMeta._recovered_settlement_resolutions = (
    _build_settlement_consumer_class_guard("_recovered_settlement_resolutions")
)
ContinuousSessionCoordinator._settlement_consumer_bindings_sealed = True
