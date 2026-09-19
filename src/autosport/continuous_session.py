from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Protocol

from .causal_collector import CausalView, DesktopDeltaConsumer
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


@dataclass(frozen=True, slots=True)
class ContinuousTickResult:
    session_id: str
    cycle_index: int
    source_id: str
    committed_delta_ids: tuple[str, ...]
    delivered_delta_ids: tuple[str, ...]
    affected_input_ids: tuple[str, ...]
    registered_input_ids: tuple[str, ...]
    retired_input_ids: tuple[str, ...]
    full_refresh_required: bool
    invalidation_backlog: bool
    settled_ticket_ids: tuple[str, ...]
    settlement_evidence_ids: tuple[str, ...]
    last_success_at: str


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


class _ContinuousSessionState:
    _SCHEMA = "autosport.continuous_session"
    _VERSION = 1
    _FIELDS = {
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
        self.source_id = _text(source_id, "source_id")
        self._clock = clock

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
            values.append(
                {
                    "event_identity": item["event_identity"],
                    "settlement_ref": item["settlement_ref"],
                    "evidence_id": item["evidence_id"],
                    "evidence_sha256": item["evidence_sha256"],
                    "available_at": item["available_at"],
                }
            )
        return tuple(values)

    def _read(self) -> dict[str, Any]:
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ContinuousSessionError(
                "cannot verify continuous session state"
            ) from exc
        if (
            type(raw) is not dict
            or set(raw) != self._FIELDS
            or raw["schema"] != self._SCHEMA
            or raw["schema_version"] != self._VERSION
            or raw["source_id"] != self.source_id
        ):
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
        raw["state"] = state.value
        raw["settlement_evidence"] = [dict(item) for item in evidence]
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
        )

    @property
    def session_id(self) -> str:
        return self._read()["session_id"]

    def _update(self, mutate: Callable[[dict[str, Any]], None]) -> None:
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

    def validate_settlement_evidence(
        self,
        *,
        settlement_evidence: tuple[SettlementResolution, ...],
    ) -> None:
        raw = self._read()
        known = {
            item["evidence_id"]: item
            for item in raw["settlement_evidence"]
        }
        for evidence in settlement_evidence:
            normalized = self._normalized_settlement_evidence(evidence)
            existing = known.get(evidence.evidence_id)
            if existing is not None and existing != normalized:
                raise ContinuousSessionError(
                    "settlement evidence id conflicts with durable evidence"
                )
            known[evidence.evidence_id] = normalized

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

            known = {
                item["evidence_id"]: item
                for item in raw["settlement_evidence"]
            }
            for evidence in settlement_evidence:
                existing = known.get(evidence.evidence_id)
                normalized = self._normalized_settlement_evidence(evidence)
                if existing is not None:
                    if existing != normalized:
                        raise ContinuousSessionError(
                            "settlement evidence id conflicts with durable evidence"
                        )
                    continue
                known[evidence.evidence_id] = normalized
            raw["settlement_evidence"] = list(
                sorted(known.values(), key=lambda item: item["evidence_id"])
            )

        self._update(mutate)

    def record_failure(self, *, code: str) -> None:
        code = _text(code, "code")
        self._update(lambda raw: raw.__setitem__("last_error_code", code))


class ContinuousSessionCoordinator:
    """Compose existing collector/lifecycle/mirror/settlement authorities into one durable loop.

    The coordinator owns only session identity/checkpoint and sequencing. It never becomes
    a market store, delta store, lifecycle store, scheduler, outcome authority, or LLM
    decision engine.
    """

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

    def status(self) -> ContinuousSessionStatus:
        return self._state.snapshot()

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

    def _settlement_resolutions(
        self,
        *,
        as_of: str,
    ) -> tuple[SettlementResolution, ...]:
        if self.outcome_authority is None:
            return ()
        resolutions: list[SettlementResolution] = []
        for record in self.lifecycle.records():
            if record.phase is not EventPhase.COMPLETED or record.settlement_ref is None:
                continue
            resolution = self.outcome_authority.resolve(record, as_of=as_of)
            if resolution is None:
                continue
            if not isinstance(resolution, SettlementResolution):
                raise ContinuousSessionError(
                    "outcome authority must return SettlementResolution or None"
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
            resolutions.append(resolution)
        return tuple(resolutions)

    def _load_book(self) -> PaperBook:
        if self.paper_book_path.exists():
            return PaperBook.load(self.paper_book_path)
        return PaperBook(self.initial_bankroll)

    def _settle(
        self,
        *,
        resolutions: tuple[SettlementResolution, ...],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not resolutions:
            return (), ()
        unique: dict[str, SettlementResolution] = {}
        for resolution in resolutions:
            unique.setdefault(resolution.evidence_id, resolution)

        with WorkspaceEconomicLock(self.workspace):
            book = self._load_book()
            engine = SettlementEngine()
            for resolution in unique.values():
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

    def tick(self) -> ContinuousTickResult:
        self._require_running()
        now = self.clock()
        _instant(now, "now")
        try:
            cycle = self.collector.run_cycle()
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

            resolutions = self._settlement_resolutions(as_of=now)
            self._state.validate_settlement_evidence(
                settlement_evidence=resolutions
            )
            settled, evidence_ids = self._settle(resolutions=resolutions)

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
            self._state.record_failure(code=type(exc).__name__)
            raise
