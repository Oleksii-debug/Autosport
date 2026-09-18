from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Callable, Protocol

from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from .ingestion_health import IngestionPolicy
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .live_observation import observe_workspace_once
from .market_mirror import MarketMirror, MirrorSnapshot
from .market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
)
from .paper import PaperBook
from .portfolio_plan import (
    PortfolioDependencyGraph,
    PortfolioPlan,
    build_portfolio_plan,
)
from .providers import MarketProvider
from .storage import SQLiteMarketStore
from .workspace_lock import WorkspaceEconomicLock


class LiveDecisionProgressError(RuntimeError):
    """Raised when persistent live-loop progress is malformed or inconsistent."""


class LiveDecisionMode(str, Enum):
    PAPER = "paper"
    SHADOW = "shadow"


class LiveControlState(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


class LiveCycleStatus(str, Enum):
    DECIDED = "decided"
    DUPLICATE_DECISION = "duplicate_decision"
    NO_CHANGE = "no_change"
    BACKPRESSURE = "backpressure"
    PROVIDER_GAP = "provider_gap"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class LiveCycleResult:
    status: LiveCycleStatus
    affected_input_ids: tuple[str, ...] = ()
    plan: PortfolioPlan | None = None
    decision_id: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LiveLoopBounds:
    observation_max_items: int = 250
    max_dirty_keys: int = 4096
    max_dirty_per_cycle: int = 250

    def __post_init__(self) -> None:
        for name in (
            "observation_max_items",
            "max_dirty_keys",
            "max_dirty_per_cycle",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive non-boolean integer")


class LiveIntentFactory(Protocol):
    def __call__(
        self,
        input_id: str,
        snapshot: MirrorSnapshot,
    ) -> tuple[object, ...]: ...


Clock = Callable[[], datetime]
ObservationRunner = Callable[[BoundedMirrorInvalidationBuffer], object]
PostAppendHook = Callable[[], None]


_PROGRESS_SCHEMA = "autosport.live_decision_progress"
_PROGRESS_VERSION = 1
_PROGRESS_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "loop_id",
        "phase",
        "decision_ts",
        "market_state_sha256",
        "affected_input_ids",
        "registered_input_ids",
        "decision_id",
        "plan_sha256",
        "gate",
    }
)
_PHASE_PENDING = "pending"
_PHASE_COMMITTED = "committed"
_GATE_NORMAL = "normal"
_GATE_PROVIDER_GAP = "provider_gap"
_SHA256_HEX = frozenset("0123456789abcdef")
_CONTROL_SCHEMA = "autosport.live_decision_control"
_CONTROL_VERSION = 1
_CONTROL_KEYS = frozenset({"schema", "schema_version", "loop_id", "state"})


def _canonical_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{name} must be a non-empty trimmed string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return value


def _canonical_timestamp(name: str, value: object) -> tuple[str, datetime]:
    raw = _canonical_text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware ISO-8601")
    return raw, parsed.astimezone(timezone.utc)


def _canonical_sha256(name: str, value: object) -> str:
    digest = _canonical_text(name, value)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in _SHA256_HEX for character in digest)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return digest


def _canonical_json_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_utc_clock(clock: Clock) -> datetime:
    now = clock()
    if not isinstance(now, datetime):
        raise TypeError("live loop clock must return datetime")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("live loop clock must return a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def _timedelta_decimal_seconds(value: timedelta) -> Decimal:
    return (
        Decimal(value.days * 86400 + value.seconds)
        + (Decimal(value.microseconds) / Decimal(1_000_000))
    )


def _conservative_timedelta(seconds: Decimal) -> timedelta:
    max_seconds = _timedelta_decimal_seconds(timedelta.max)
    if seconds >= max_seconds:
        return timedelta.max
    microseconds = int(
        (seconds * Decimal(1_000_000)).to_integral_value(rounding=ROUND_FLOOR)
    )
    return timedelta(microseconds=microseconds)


@dataclass(frozen=True, slots=True)
class _Control:
    loop_id: str
    state: LiveControlState

    def __post_init__(self) -> None:
        _canonical_text("loop_id", self.loop_id)
        if not isinstance(self.state, LiveControlState):
            raise LiveDecisionProgressError("live control state is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _CONTROL_SCHEMA,
            "schema_version": _CONTROL_VERSION,
            "loop_id": self.loop_id,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "_Control":
        if type(raw) is not dict or set(raw) != _CONTROL_KEYS:
            raise LiveDecisionProgressError(
                "live decision control must contain canonical fields"
            )
        if raw["schema"] != _CONTROL_SCHEMA or raw["schema_version"] != _CONTROL_VERSION:
            raise LiveDecisionProgressError("unsupported live decision control schema")
        try:
            return cls(
                loop_id=raw["loop_id"],
                state=LiveControlState(raw["state"]),
            )
        except (TypeError, ValueError) as exc:
            raise LiveDecisionProgressError("live decision control is invalid") from exc


@dataclass(frozen=True, slots=True)
class _Progress:
    loop_id: str
    phase: str
    decision_ts: str
    market_state_sha256: str
    affected_input_ids: tuple[str, ...]
    registered_input_ids: tuple[str, ...]
    decision_id: str | None
    plan_sha256: str | None
    gate: str

    def __post_init__(self) -> None:
        _canonical_text("loop_id", self.loop_id)
        _canonical_timestamp("decision_ts", self.decision_ts)
        _canonical_sha256("market_state_sha256", self.market_state_sha256)
        if self.phase not in {_PHASE_PENDING, _PHASE_COMMITTED}:
            raise LiveDecisionProgressError("unsupported live progress phase")
        if self.gate not in {_GATE_NORMAL, _GATE_PROVIDER_GAP}:
            raise LiveDecisionProgressError("unsupported live progress gate")
        if type(self.affected_input_ids) is not tuple:
            raise LiveDecisionProgressError("affected_input_ids must be a tuple")
        for input_id in self.affected_input_ids:
            _canonical_text("affected input id", input_id)
        if len(self.affected_input_ids) != len(set(self.affected_input_ids)):
            raise LiveDecisionProgressError("affected_input_ids must be unique")
        if type(self.registered_input_ids) is not tuple:
            raise LiveDecisionProgressError("registered_input_ids must be a tuple")
        for input_id in self.registered_input_ids:
            _canonical_text("registered input id", input_id)
        if len(self.registered_input_ids) != len(set(self.registered_input_ids)):
            raise LiveDecisionProgressError("registered_input_ids must be unique")
        if not set(self.affected_input_ids).issubset(self.registered_input_ids):
            raise LiveDecisionProgressError(
                "affected_input_ids must be a subset of registered_input_ids"
            )
        if self.phase == _PHASE_PENDING:
            if self.decision_id is not None or self.plan_sha256 is not None:
                raise LiveDecisionProgressError(
                    "pending live progress cannot claim a durable decision"
                )
        else:
            if self.decision_id is None or self.plan_sha256 is None:
                raise LiveDecisionProgressError(
                    "committed live progress requires decision and plan identity"
                )
            _canonical_text("decision_id", self.decision_id)
            _canonical_sha256("plan_sha256", self.plan_sha256)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _PROGRESS_SCHEMA,
            "schema_version": _PROGRESS_VERSION,
            "loop_id": self.loop_id,
            "phase": self.phase,
            "decision_ts": self.decision_ts,
            "market_state_sha256": self.market_state_sha256,
            "affected_input_ids": list(self.affected_input_ids),
            "registered_input_ids": list(self.registered_input_ids),
            "decision_id": self.decision_id,
            "plan_sha256": self.plan_sha256,
            "gate": self.gate,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "_Progress":
        if type(raw) is not dict or set(raw) != _PROGRESS_KEYS:
            raise LiveDecisionProgressError(
                "live decision progress must contain canonical fields"
            )
        if raw["schema"] != _PROGRESS_SCHEMA or raw["schema_version"] != _PROGRESS_VERSION:
            raise LiveDecisionProgressError("unsupported live decision progress schema")
        input_ids = raw["affected_input_ids"]
        registered_ids = raw["registered_input_ids"]
        if type(input_ids) is not list or any(type(value) is not str for value in input_ids):
            raise LiveDecisionProgressError(
                "affected_input_ids must be a JSON string array"
            )
        if type(registered_ids) is not list or any(
            type(value) is not str for value in registered_ids
        ):
            raise LiveDecisionProgressError(
                "registered_input_ids must be a JSON string array"
            )
        try:
            return cls(
                loop_id=raw["loop_id"],
                phase=raw["phase"],
                decision_ts=raw["decision_ts"],
                market_state_sha256=raw["market_state_sha256"],
                affected_input_ids=tuple(input_ids),
                registered_input_ids=tuple(registered_ids),
                decision_id=raw["decision_id"],
                plan_sha256=raw["plan_sha256"],
                gate=raw["gate"],
            )
        except (TypeError, ValueError) as exc:
            raise LiveDecisionProgressError("live decision progress is invalid") from exc


class PersistentLiveDecisionLoop:
    """Bounded, restart-safe paper/shadow decision loop over canonical authorities.

    Market persistence remains owned by SQLiteMarketStore/MarketEventBus. MarketMirror
    and its dirty buffer are projections only. Opportunity construction is supplied by
    a deterministic strategy-specific factory. Whole-portfolio arithmetic remains owned
    by build_portfolio_plan/PaperRiskPolicy. Economic decisions are appended to the
    canonical JsonlDecisionLedger under WorkspaceEconomicLock.

    The progress JSON is only a crash-recovery cursor. It never stores quote values,
    bankroll state, risk limits, or settlement truth.
    """

    PROGRESS_FILE_NAME = "live_decision_progress.json"
    CONTROL_FILE_NAME = "live_decision_control.json"
    AGENT_ID = "persistent-live-decision-loop"

    def __init__(
        self,
        workspace: str | Path,
        *,
        loop_id: str,
        mode: LiveDecisionMode,
        book: PaperBook,
        authority: EconomicDecisionAuthority,
        intent_factory: LiveIntentFactory,
        provider: MarketProvider | None = None,
        decision_ledger: JsonlDecisionLedger | None = None,
        ingestion_policy: IngestionPolicy | None = None,
        max_quote_age: timedelta | None = None,
        bounds: LiveLoopBounds | None = None,
        clock: Clock | None = None,
        observation_runner: ObservationRunner | None = None,
        post_append_hook: PostAppendHook | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.loop_id = _canonical_text("loop_id", loop_id)
        if not isinstance(mode, LiveDecisionMode):
            raise TypeError("mode must be LiveDecisionMode")
        if not isinstance(book, PaperBook):
            raise TypeError("book must be PaperBook")
        if not isinstance(authority, EconomicDecisionAuthority):
            raise TypeError("authority must be EconomicDecisionAuthority")
        if not callable(intent_factory):
            raise TypeError("intent_factory must be callable")
        goal_quote_age = authority.contract.max_quote_age_seconds
        if max_quote_age is None:
            max_quote_age = _conservative_timedelta(goal_quote_age)
        elif not isinstance(max_quote_age, timedelta) or max_quote_age < timedelta(0):
            raise ValueError("max_quote_age must be a non-negative timedelta or None")
        elif _timedelta_decimal_seconds(max_quote_age) > goal_quote_age:
            raise ValueError(
                "max_quote_age cannot exceed EconomicGoalContract.max_quote_age_seconds"
            )
        if observation_runner is None and provider is None:
            raise ValueError("provider is required when observation_runner is omitted")

        self.mode = mode
        self.book = book
        self.authority = authority
        self.intent_factory = intent_factory
        self.provider = provider
        self.decision_ledger = decision_ledger or JsonlDecisionLedger(
            self.workspace / "decisions.jsonl"
        )
        self.ingestion_policy = ingestion_policy
        self.max_quote_age = max_quote_age
        self.bounds = bounds or LiveLoopBounds()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.post_append_hook = post_append_hook

        store = SQLiteMarketStore(self.workspace / "market.db")
        try:
            mirror = MarketMirror.from_store(store)
        finally:
            store.close()
        self.mirror_updates = BoundedMirrorInvalidationBuffer(
            mirror,
            max_dirty_keys=self.bounds.max_dirty_keys,
        )
        self.dependencies = FocusedMirrorDependencyIndex(mirror)
        if observation_runner is None:
            assert provider is not None

            def _default_observer(
                updates: BoundedMirrorInvalidationBuffer,
            ) -> object:
                return observe_workspace_once(
                    self.workspace,
                    provider,
                    max_items=self.bounds.observation_max_items,
                    policy=self.ingestion_policy,
                    mirror_updates=updates,
                )

            self._observe = _default_observer
        else:
            self._observe = observation_runner

        self.progress_path = self.workspace / self.PROGRESS_FILE_NAME
        self.control_path = self.workspace / self.CONTROL_FILE_NAME
        self._progress = self._load_progress()
        if self._progress is not None and self._progress.loop_id != self.loop_id:
            raise LiveDecisionProgressError(
                "persisted live progress belongs to a different loop_id"
            )
        durable_control = self._load_control()
        if durable_control is None:
            durable_control = _Control(self.loop_id, LiveControlState.RUNNING)
        elif durable_control.loop_id != self.loop_id:
            raise LiveDecisionProgressError(
                "persisted live control belongs to a different loop_id"
            )
        self._control = durable_control
        self._intent_cache: dict[str, tuple[object, ...]] = {}
        self._pending_affected: dict[str, None] = {}
        self._needs_cache_rebuild = True
        self._next_freshness_deadline: datetime | None = None

    @property
    def paused(self) -> bool:
        return self._control.state is LiveControlState.PAUSED

    @property
    def stopped(self) -> bool:
        return self._control.state is LiveControlState.STOPPED

    def register_input(
        self,
        input_id: str,
        *,
        source_ids: str | tuple[str, ...] | None = None,
        event_ids: str | tuple[str, ...] | None = None,
        market_ids: str | tuple[str, ...] | None = None,
        selection_ids: str | tuple[str, ...] | None = None,
    ) -> None:
        dependency = self.dependencies.register(
            input_id,
            source_ids=source_ids,
            event_ids=event_ids,
            market_ids=market_ids,
            selection_ids=selection_ids,
        )
        self._pending_affected[dependency.input_id] = None
        self._needs_cache_rebuild = True

    def pause(self) -> None:
        if self.stopped:
            raise RuntimeError("cannot pause a stopped live loop")
        self._persist_control(LiveControlState.PAUSED)

    def resume(self) -> None:
        if self.stopped:
            raise RuntimeError("cannot resume a stopped live loop")
        self._persist_control(LiveControlState.RUNNING)

    def stop(self) -> None:
        self._persist_control(LiveControlState.STOPPED)

    def run(
        self,
        *,
        max_cycles: int,
        deadline_seconds: float | None = None,
    ) -> tuple[LiveCycleResult, ...]:
        if type(max_cycles) is not int or max_cycles <= 0:
            raise ValueError("max_cycles must be a positive non-boolean integer")
        if deadline_seconds is not None:
            if isinstance(deadline_seconds, bool) or not isinstance(
                deadline_seconds, (int, float)
            ):
                raise TypeError("deadline_seconds must be a positive number or None")
            if deadline_seconds <= 0:
                raise ValueError("deadline_seconds must be positive")
        deadline = None if deadline_seconds is None else monotonic() + deadline_seconds
        results: list[LiveCycleResult] = []
        for _ in range(max_cycles):
            if deadline is not None and monotonic() >= deadline:
                break
            result = self.run_cycle()
            results.append(result)
            if result.status is LiveCycleStatus.STOPPED:
                break
        return tuple(results)

    def run_cycle(self) -> LiveCycleResult:
        if self.stopped:
            return LiveCycleResult(
                LiveCycleStatus.STOPPED,
                detail="durable STOP is latched; provider was not polled",
            )
        if self.paused:
            return LiveCycleResult(
                LiveCycleStatus.PAUSED,
                detail="durable PAUSE is active; provider was not polled",
            )

        now = _require_utc_clock(self.clock)
        try:
            self._observe(self.mirror_updates)
        except Exception as exc:
            self._needs_cache_rebuild = True
            return self._persist_provider_gap(now, exc)

        batch = self.mirror_updates.drain(
            max_items=self.bounds.max_dirty_per_cycle
        )
        batch_affected = self.dependencies.affected_inputs(batch)
        for input_id in batch_affected:
            self._pending_affected[input_id] = None
        if batch.full_refresh_required:
            self._pending_affected = {
                input_id: None for input_id in self.dependencies.input_ids
            }
            self._needs_cache_rebuild = True

        if batch.has_more:
            return LiveCycleResult(
                LiveCycleStatus.BACKPRESSURE,
                affected_input_ids=tuple(self._pending_affected),
                detail=(
                    "bounded invalidation drain has more work; no economic action "
                    "was emitted from a partial dirty set"
                ),
            )

        freshness_expired = (
            self._next_freshness_deadline is not None
            and now > self._next_freshness_deadline
        )
        if freshness_expired:
            self._needs_cache_rebuild = True

        registered_input_ids = self.dependencies.input_ids
        current_market_sha = self._market_state_sha256()
        recovering_pending = (
            self._progress is not None
            and self._progress.phase == _PHASE_PENDING
            and self._progress.gate == _GATE_NORMAL
            and self._progress.market_state_sha256 == current_market_sha
            and self._progress.registered_input_ids == registered_input_ids
        )
        if recovering_pending:
            self._needs_cache_rebuild = True

        clean_committed_restart = (
            self._needs_cache_rebuild
            and self._progress is not None
            and self._progress.phase == _PHASE_COMMITTED
            and self._progress.gate == _GATE_NORMAL
            and self._progress.market_state_sha256 == current_market_sha
            and self._progress.registered_input_ids == registered_input_ids
            and not batch_affected
            and not batch.full_refresh_required
            and not freshness_expired
        )
        if clean_committed_restart:
            assert self._progress is not None
            _, previous_decision_time = _canonical_timestamp(
                "committed decision_ts", self._progress.decision_ts
            )
            if self._active_views_equal(previous_decision_time, now):
                self._refresh_intents(registered_input_ids, now)
                self._pending_affected.clear()
                self._needs_cache_rebuild = False
                self._update_freshness_deadline(now)
                return LiveCycleResult(
                    LiveCycleStatus.NO_CHANGE,
                    detail=(
                        "clean restart rebuilt deterministic intent cache from the "
                        "same canonical decision-visible market state"
                    ),
                )

        if self._needs_cache_rebuild:
            for input_id in registered_input_ids:
                self._pending_affected[input_id] = None

        refresh_input_ids = tuple(self._pending_affected)
        if recovering_pending:
            assert self._progress is not None
            affected = self._progress.affected_input_ids
        else:
            affected = refresh_input_ids
        if not affected and not refresh_input_ids:
            return LiveCycleResult(
                LiveCycleStatus.NO_CHANGE,
                detail="no material quote, status, dependency, or freshness invalidation",
            )

        if recovering_pending:
            assert self._progress is not None
            decision_ts, decision_time = _canonical_timestamp(
                "pending decision_ts", self._progress.decision_ts
            )
        else:
            decision_time = now
            decision_ts = now.isoformat()

        self._write_pending(
            decision_ts=decision_ts,
            market_state_sha256=current_market_sha,
            affected_input_ids=affected,
            gate=_GATE_NORMAL,
        )
        self._refresh_intents(refresh_input_ids, decision_time)

        intents = self._all_cached_intents()
        graph = (
            None
            if not intents
            else PortfolioDependencyGraph.for_inputs(self.book, intents)
        )
        plan = build_portfolio_plan(
            self.book,
            intents,
            self.authority.risk_policy,
            decision_ts,
            dependency_graph=graph,
            market_outcome_authorities=(),
        )
        result = self._persist_plan(
            plan=plan,
            market_state_sha256=current_market_sha,
            affected_input_ids=affected,
            gate=_GATE_NORMAL,
        )
        self._pending_affected.clear()
        self._needs_cache_rebuild = False
        self._update_freshness_deadline(decision_time)
        return result

    def _active_views_equal(
        self,
        earlier: datetime,
        later: datetime,
    ) -> bool:
        for input_id in self.dependencies.input_ids:
            earlier_view = self.dependencies.decision_view(
                input_id,
                as_of=earlier,
                max_age=self.max_quote_age,
            )
            later_view = self.dependencies.decision_view(
                input_id,
                as_of=later,
                max_age=self.max_quote_age,
            )
            if tuple(event.to_dict() for event in earlier_view.events) != tuple(
                event.to_dict() for event in later_view.events
            ):
                return False
        return True

    def _refresh_intents(
        self,
        input_ids: tuple[str, ...],
        as_of: datetime,
    ) -> None:
        from .portfolio_plan import OpportunityIntent

        for input_id in input_ids:
            snapshot = self.dependencies.decision_view(
                input_id,
                as_of=as_of,
                max_age=self.max_quote_age,
            )
            produced = self.intent_factory(input_id, snapshot)
            if type(produced) is not tuple:
                raise TypeError("intent_factory must return a tuple")
            if any(not isinstance(intent, OpportunityIntent) for intent in produced):
                raise TypeError(
                    "intent_factory must return only canonical OpportunityIntent values"
                )
            self._intent_cache[input_id] = produced

    def _all_cached_intents(self) -> tuple[object, ...]:
        flattened: list[object] = []
        for input_id in self.dependencies.input_ids:
            flattened.extend(self._intent_cache.get(input_id, ()))
        return tuple(flattened)

    def _update_freshness_deadline(self, as_of: datetime) -> None:
        deadlines: list[datetime] = []
        for input_id in self.dependencies.input_ids:
            snapshot = self.dependencies.decision_view(
                input_id,
                as_of=as_of,
                max_age=self.max_quote_age,
            )
            for event in snapshot.events:
                raw = event.source_ts or event.observed_ts
                try:
                    timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    continue
                deadlines.append(
                    timestamp.astimezone(timezone.utc) + self.max_quote_age
                )
        self._next_freshness_deadline = min(deadlines) if deadlines else None

    def _persist_provider_gap(
        self,
        now: datetime,
        exc: Exception,
    ) -> LiveCycleResult:
        decision_ts = now.isoformat()
        market_sha = self._market_state_sha256()
        affected = self.dependencies.input_ids
        self._write_pending(
            decision_ts=decision_ts,
            market_state_sha256=market_sha,
            affected_input_ids=affected,
            gate=_GATE_PROVIDER_GAP,
        )
        plan = build_portfolio_plan(
            self.book,
            (),
            self.authority.risk_policy,
            decision_ts,
            dependency_graph=None,
            market_outcome_authorities=(),
        )
        result = self._persist_plan(
            plan=plan,
            market_state_sha256=market_sha,
            affected_input_ids=affected,
            gate=_GATE_PROVIDER_GAP,
            detail=(
                "provider observation failed closed as a ZERO paper/shadow plan: "
                f"{type(exc).__name__}"
            ),
        )
        return LiveCycleResult(
            status=LiveCycleStatus.PROVIDER_GAP,
            affected_input_ids=result.affected_input_ids,
            plan=result.plan,
            decision_id=result.decision_id,
            detail=result.detail,
        )

    def _persist_plan(
        self,
        *,
        plan: PortfolioPlan,
        market_state_sha256: str,
        affected_input_ids: tuple[str, ...],
        gate: str,
        detail: str = "",
    ) -> LiveCycleResult:
        context_payload = {
            "schema": "autosport.live_decision_context",
            "schema_version": 1,
            "loop_id": self.loop_id,
            "mode": self.mode.value,
            "gate": gate,
            "market_state_sha256": market_state_sha256,
            "plan_sha256": plan.plan_sha256,
        }
        context_hash = _canonical_json_sha256(context_payload)
        decision_id = f"live-{context_hash}"
        record = DecisionRecord(
            replay_run_id=f"live:{self.loop_id}",
            agent=self.AGENT_ID,
            observed_ts=plan.decision_ts,
            action=f"LIVE_{plan.action.value.upper()}",
            payload={
                "schema": "autosport.persistent_live_decision",
                "schema_version": 1,
                "loop_id": self.loop_id,
                "mode": self.mode.value,
                "gate": gate,
                "market_state_sha256": market_state_sha256,
                "affected_input_ids": list(affected_input_ids),
                "plan_sha256": plan.plan_sha256,
                "plan": plan.to_dict(),
                MATERIAL_ACTION_ID_PAYLOAD_KEY: decision_id,
            },
            context_hash=context_hash,
            decision_id=decision_id,
            decision_kind=ECONOMIC_DECISION_KIND,
        )

        duplicate = False
        with WorkspaceEconomicLock(self.workspace):
            durable_progress = self._load_progress()
            if (
                durable_progress is None
                or durable_progress.phase != _PHASE_PENDING
                or durable_progress.loop_id != self.loop_id
                or durable_progress.decision_ts != plan.decision_ts
                or durable_progress.market_state_sha256 != market_state_sha256
                or durable_progress.affected_input_ids != affected_input_ids
                or durable_progress.registered_input_ids != self.dependencies.input_ids
                or durable_progress.gate != gate
            ):
                raise LiveDecisionProgressError(
                    "live decision progress changed before durable ledger publication"
                )
            existing = None
            records = (
                self.decision_ledger.verified_records()
                if self.decision_ledger.path.exists()
                else ()
            )
            for item in records:
                if item.decision_id == decision_id:
                    existing = item
                    break
            if existing is not None:
                if (
                    existing.context_hash != context_hash
                    or existing.payload.get("plan_sha256") != plan.plan_sha256
                    or existing.payload.get("market_state_sha256")
                    != market_state_sha256
                    or existing.payload.get("gate") != gate
                ):
                    raise DecisionLedgerIntegrityError(
                        "existing live decision identity conflicts with recomputed evidence"
                    )
                duplicate = True
            else:
                self.decision_ledger.append_economic(
                    record,
                    self.authority,
                )
                if self.post_append_hook is not None:
                    self.post_append_hook()

            committed = _Progress(
                loop_id=self.loop_id,
                phase=_PHASE_COMMITTED,
                decision_ts=plan.decision_ts,
                market_state_sha256=market_state_sha256,
                affected_input_ids=affected_input_ids,
                registered_input_ids=self.dependencies.input_ids,
                decision_id=decision_id,
                plan_sha256=plan.plan_sha256,
                gate=gate,
            )
            atomic_write_json(self.progress_path, committed.to_dict())
            self._progress = committed

        return LiveCycleResult(
            LiveCycleStatus.DUPLICATE_DECISION if duplicate else LiveCycleStatus.DECIDED,
            affected_input_ids=affected_input_ids,
            plan=plan,
            decision_id=decision_id,
            detail=detail,
        )

    def _write_pending(
        self,
        *,
        decision_ts: str,
        market_state_sha256: str,
        affected_input_ids: tuple[str, ...],
        gate: str,
    ) -> None:
        pending = _Progress(
            loop_id=self.loop_id,
            phase=_PHASE_PENDING,
            decision_ts=decision_ts,
            market_state_sha256=market_state_sha256,
            affected_input_ids=affected_input_ids,
            registered_input_ids=self.dependencies.input_ids,
            decision_id=None,
            plan_sha256=None,
            gate=gate,
        )
        with WorkspaceEconomicLock(self.workspace):
            atomic_write_json(self.progress_path, pending.to_dict())
        self._progress = pending

    def _persist_control(self, state: LiveControlState) -> None:
        candidate = _Control(self.loop_id, state)
        with WorkspaceEconomicLock(self.workspace):
            durable = self._load_control()
            if durable is not None:
                if durable.loop_id != self.loop_id:
                    raise LiveDecisionProgressError(
                        "persisted live control belongs to a different loop_id"
                    )
                if (
                    durable.state is LiveControlState.STOPPED
                    and state is not LiveControlState.STOPPED
                ):
                    raise RuntimeError("durable STOP cannot be cleared by this loop")
            atomic_write_json(self.control_path, candidate.to_dict())
        self._control = candidate

    def _load_control(self) -> _Control | None:
        if not self.control_path.exists():
            return None
        try:
            text = self.control_path.read_text(encoding="utf-8")
            raw = strict_json_loads(text)
            return _Control.from_dict(raw)
        except (OSError, TypeError, ValueError) as exc:
            raise LiveDecisionProgressError(
                "cannot verify persisted live decision control"
            ) from exc

    def _load_progress(self) -> _Progress | None:
        if not self.progress_path.exists():
            return None
        try:
            text = self.progress_path.read_text(encoding="utf-8")
            raw = strict_json_loads(text)
            return _Progress.from_dict(raw)
        except (OSError, TypeError, ValueError) as exc:
            raise LiveDecisionProgressError(
                "cannot verify persisted live decision progress"
            ) from exc

    def _market_state_sha256(self) -> str:
        payload = [event.to_dict() for event in self.mirror_updates.mirror.snapshot()]
        return _canonical_json_sha256(payload)
