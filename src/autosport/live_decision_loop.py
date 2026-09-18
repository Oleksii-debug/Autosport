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
    verify_economic_goal_binding,
)
from .ingestion_health import IngestionPolicy
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .live_observation import observe_workspace_once
from .market_mirror import MarketMirror, MirrorSnapshot
from .market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependency,
    FocusedMirrorDependencyIndex,
)
from .paper import PaperBook
from .portfolio_plan import (
    PortfolioDependencyGraph,
    PortfolioPlan,
    build_portfolio_plan,
)
from .providers import MarketProvider, ProviderUnavailableError
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
    max_registered_inputs: int = 4096

    def __post_init__(self) -> None:
        for name in (
            "observation_max_items",
            "max_dirty_keys",
            "max_dirty_per_cycle",
            "max_registered_inputs",
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
        "ledger_offset",
        "gate",
    }
)
_PHASE_PENDING = "pending"
_PHASE_APPEND_PENDING = "append_pending"
_PHASE_COMMITTED = "committed"
_GATE_NORMAL = "normal"
_GATE_PROVIDER_GAP = "provider_gap"
_SHA256_HEX = frozenset("0123456789abcdef")
_CONTROL_SCHEMA = "autosport.live_decision_control"
_CONTROL_VERSION = 1
_CONTROL_KEYS = frozenset({"schema", "schema_version", "loop_id", "state"})
_INPUTS_SCHEMA = "autosport.live_decision_inputs"
_INPUTS_VERSION = 1
_INPUTS_KEYS = frozenset({"schema", "schema_version", "loop_id", "inputs"})
_INPUT_SPEC_KEYS = frozenset(
    {"input_id", "source_ids", "event_ids", "market_ids", "selection_ids"}
)


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


def _selector_tuple(
    values: str | tuple[str, ...] | None,
    *,
    name: str,
) -> tuple[str, ...] | None:
    normalized = FocusedMirrorDependencyIndex._selector(values, name=name)
    return None if normalized is None else tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class _InputSpec:
    input_id: str
    source_ids: tuple[str, ...] | None
    event_ids: tuple[str, ...] | None
    market_ids: tuple[str, ...] | None
    selection_ids: tuple[str, ...] | None

    def __post_init__(self) -> None:
        FocusedMirrorDependencyIndex._input_id(self.input_id)
        for name in ("source_ids", "event_ids", "market_ids", "selection_ids"):
            values = getattr(self, name)
            if values is None:
                continue
            if type(values) is not tuple or values != tuple(sorted(set(values))):
                raise LiveDecisionProgressError(
                    f"{name} must be a sorted unique selector tuple"
                )
            FocusedMirrorDependencyIndex._selector(values, name=name)

    @classmethod
    def from_dependency(cls, dependency: FocusedMirrorDependency) -> "_InputSpec":
        return cls(
            input_id=dependency.input_id,
            source_ids=(
                None
                if dependency.source_ids is None
                else tuple(sorted(dependency.source_ids))
            ),
            event_ids=(
                None
                if dependency.event_ids is None
                else tuple(sorted(dependency.event_ids))
            ),
            market_ids=(
                None
                if dependency.market_ids is None
                else tuple(sorted(dependency.market_ids))
            ),
            selection_ids=(
                None
                if dependency.selection_ids is None
                else tuple(sorted(dependency.selection_ids))
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "input_id": self.input_id,
            "source_ids": None if self.source_ids is None else list(self.source_ids),
            "event_ids": None if self.event_ids is None else list(self.event_ids),
            "market_ids": None if self.market_ids is None else list(self.market_ids),
            "selection_ids": (
                None if self.selection_ids is None else list(self.selection_ids)
            ),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "_InputSpec":
        if type(raw) is not dict or set(raw) != _INPUT_SPEC_KEYS:
            raise LiveDecisionProgressError(
                "live dependency input must contain canonical fields"
            )

        def selector(name: str) -> tuple[str, ...] | None:
            value = raw[name]
            if value is None:
                return None
            if type(value) is not list or any(type(item) is not str for item in value):
                raise LiveDecisionProgressError(
                    f"live dependency {name} must be null or a string array"
                )
            return tuple(value)

        try:
            return cls(
                input_id=raw["input_id"],
                source_ids=selector("source_ids"),
                event_ids=selector("event_ids"),
                market_ids=selector("market_ids"),
                selection_ids=selector("selection_ids"),
            )
        except (TypeError, ValueError) as exc:
            raise LiveDecisionProgressError(
                "live dependency input is invalid"
            ) from exc


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
    ledger_offset: int | None
    gate: str

    def __post_init__(self) -> None:
        _canonical_text("loop_id", self.loop_id)
        _canonical_timestamp("decision_ts", self.decision_ts)
        _canonical_sha256("market_state_sha256", self.market_state_sha256)
        if self.phase not in {
            _PHASE_PENDING,
            _PHASE_APPEND_PENDING,
            _PHASE_COMMITTED,
        }:
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
            if (
                self.decision_id is not None
                or self.plan_sha256 is not None
                or self.ledger_offset is not None
            ):
                raise LiveDecisionProgressError(
                    "pending live progress cannot claim append identity"
                )
        else:
            if self.decision_id is None or self.plan_sha256 is None:
                raise LiveDecisionProgressError(
                    "append/committed progress requires decision and plan identity"
                )
            _canonical_text("decision_id", self.decision_id)
            _canonical_sha256("plan_sha256", self.plan_sha256)
            if (
                isinstance(self.ledger_offset, bool)
                or not isinstance(self.ledger_offset, int)
                or self.ledger_offset < 0
            ):
                raise LiveDecisionProgressError(
                    "append/committed progress requires non-negative ledger_offset"
                )

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
            "ledger_offset": self.ledger_offset,
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
                ledger_offset=raw["ledger_offset"],
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
    INPUTS_FILE_NAME = "live_decision_inputs.json"
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
        self.inputs_path = self.workspace / self.INPUTS_FILE_NAME
        durable_input_specs = self._load_input_registry() or ()
        if len(durable_input_specs) > self.bounds.max_registered_inputs:
            raise LiveDecisionProgressError(
                "durable live dependency registry exceeds max_registered_inputs"
            )
        self._input_specs: dict[str, _InputSpec] = {}
        for spec in durable_input_specs:
            dependency = self.dependencies.register(
                spec.input_id,
                source_ids=spec.source_ids,
                event_ids=spec.event_ids,
                market_ids=spec.market_ids,
                selection_ids=spec.selection_ids,
            )
            restored = _InputSpec.from_dependency(dependency)
            if restored != spec:
                raise LiveDecisionProgressError(
                    "durable live dependency registry changed during restoration"
                )
            self._input_specs[spec.input_id] = spec

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
        if self.decision_ledger.path.exists():
            with WorkspaceEconomicLock(self.workspace):
                self.decision_ledger.verify_integrity()
                if (
                    self._progress is not None
                    and self._progress.phase == _PHASE_COMMITTED
                ):
                    self._verify_committed_progress_ledger_binding(self._progress)
        elif (
            self._progress is not None
            and self._progress.phase == _PHASE_COMMITTED
        ):
            raise DecisionLedgerIntegrityError(
                "Decision Ledger file is missing or unreadable"
            )
        if self._progress is not None:
            durable_input_ids = set(self.dependencies.input_ids)
            progress_input_ids = set(self._progress.registered_input_ids)
            if not progress_input_ids.issubset(durable_input_ids):
                raise LiveDecisionProgressError(
                    "persisted live progress references missing durable dependency inputs"
                )
            if (
                self._progress.phase == _PHASE_APPEND_PENDING
                and self._progress.registered_input_ids != self.dependencies.input_ids
            ):
                raise LiveDecisionProgressError(
                    "unfinished ledger append requires exact durable dependency registry"
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
        normalized_id = FocusedMirrorDependencyIndex._input_id(input_id)
        candidate = _InputSpec(
            input_id=normalized_id,
            source_ids=_selector_tuple(source_ids, name="source_ids"),
            event_ids=_selector_tuple(event_ids, name="event_ids"),
            market_ids=_selector_tuple(market_ids, name="market_ids"),
            selection_ids=_selector_tuple(selection_ids, name="selection_ids"),
        )
        existing = self._input_specs.get(normalized_id)
        if existing is not None:
            if existing != candidate:
                raise ValueError(
                    f"input_id {normalized_id!r} conflicts with durable registration"
                )
            return
        if len(self._input_specs) >= self.bounds.max_registered_inputs:
            raise ValueError("max_registered_inputs would be exceeded")

        previous_specs = tuple(self._input_specs.values())
        dependency = self.dependencies.register(
            candidate.input_id,
            source_ids=candidate.source_ids,
            event_ids=candidate.event_ids,
            market_ids=candidate.market_ids,
            selection_ids=candidate.selection_ids,
        )
        self._input_specs[candidate.input_id] = candidate
        try:
            self._persist_input_registry(expected_previous=previous_specs)
        except BaseException:
            self._input_specs.pop(candidate.input_id, None)
            self.dependencies.unregister(candidate.input_id)
            raise
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

        if (
            self._progress is not None
            and self._progress.phase in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
        ):
            return self._recover_unfinished_progress()

        try:
            self._observe(self.mirror_updates)
        except ProviderUnavailableError as exc:
            self._needs_cache_rebuild = True
            return self._persist_provider_gap(
                _require_utc_clock(self.clock),
                exc,
            )

        now = _require_utc_clock(self.clock)
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
        affected = refresh_input_ids
        if not affected:
            return LiveCycleResult(
                LiveCycleStatus.NO_CHANGE,
                detail="no material quote, status, dependency, or freshness invalidation",
            )

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

    def _recover_unfinished_progress(self) -> LiveCycleResult:
        progress = self._progress
        if progress is None or progress.phase not in {
            _PHASE_PENDING,
            _PHASE_APPEND_PENDING,
        }:
            raise LiveDecisionProgressError(
                "unfinished live decision recovery requires pending progress"
            )
        if progress.registered_input_ids != self.dependencies.input_ids:
            raise LiveDecisionProgressError(
                "unfinished live decision requires exact durable dependency registry"
            )

        decision_ts, decision_time = _canonical_timestamp(
            "pending decision_ts",
            progress.decision_ts,
        )
        if progress.gate == _GATE_NORMAL:
            self._refresh_intents_from_replay(
                progress.registered_input_ids,
                decision_time,
            )
            intents = self._all_cached_intents()
            graph = (
                None
                if not intents
                else PortfolioDependencyGraph.for_inputs(self.book, intents)
            )
        else:
            intents = ()
            graph = None

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
            market_state_sha256=progress.market_state_sha256,
            affected_input_ids=progress.affected_input_ids,
            gate=progress.gate,
            detail=(
                "recovered unfinished durable live decision before provider polling"
            ),
        )
        self._pending_affected.clear()
        self._needs_cache_rebuild = True
        self._next_freshness_deadline = None
        return result

    def _refresh_intents_from_replay(
        self,
        input_ids: tuple[str, ...],
        as_of: datetime,
    ) -> None:
        from .portfolio_plan import OpportunityIntent

        store = SQLiteMarketStore(self.workspace / "market.db")
        try:
            snapshot = MarketMirror.replay_view_from_store(
                store,
                as_of=as_of,
                max_age=self.max_quote_age,
            )
        finally:
            store.close()

        for input_id in input_ids:
            try:
                spec = self._input_specs[input_id]
            except KeyError as exc:
                raise LiveDecisionProgressError(
                    "unfinished live decision references missing durable input"
                ) from exc
            focused = MirrorSnapshot(
                revision=snapshot.revision,
                events=tuple(
                    event
                    for event in snapshot.events
                    if (
                        (spec.source_ids is None or event.source_id in spec.source_ids)
                        and (spec.event_ids is None or event.event_id in spec.event_ids)
                        and (spec.market_ids is None or event.market_id in spec.market_ids)
                        and (
                            spec.selection_ids is None
                            or event.selection_id in spec.selection_ids
                        )
                    )
                ),
            )
            produced = self.intent_factory(input_id, focused)
            if type(produced) is not tuple:
                raise TypeError("intent_factory must return a tuple")
            if any(not isinstance(intent, OpportunityIntent) for intent in produced):
                raise TypeError(
                    "intent_factory must return only canonical OpportunityIntent values"
                )
            self._intent_cache[input_id] = produced

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
                or durable_progress.phase
                not in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
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

            if durable_progress.phase == _PHASE_APPEND_PENDING:
                if (
                    durable_progress.decision_id != decision_id
                    or durable_progress.plan_sha256 != plan.plan_sha256
                    or durable_progress.ledger_offset is None
                ):
                    raise LiveDecisionProgressError(
                        "reserved ledger append identity conflicts with recomputed plan"
                    )
                ledger_offset = durable_progress.ledger_offset
            else:
                ledger_offset = self._ledger_end_offset()
                durable_progress = _Progress(
                    loop_id=self.loop_id,
                    phase=_PHASE_APPEND_PENDING,
                    decision_ts=plan.decision_ts,
                    market_state_sha256=market_state_sha256,
                    affected_input_ids=affected_input_ids,
                    registered_input_ids=self.dependencies.input_ids,
                    decision_id=decision_id,
                    plan_sha256=plan.plan_sha256,
                    ledger_offset=ledger_offset,
                    gate=gate,
                )
                atomic_write_json(self.progress_path, durable_progress.to_dict())
                self._progress = durable_progress

            existing = self._verified_ledger_record_at_offset(ledger_offset)
            if existing is not None and existing.decision_id != decision_id:
                ledger_offset = self._ledger_end_offset()
                durable_progress = _Progress(
                    loop_id=self.loop_id,
                    phase=_PHASE_APPEND_PENDING,
                    decision_ts=plan.decision_ts,
                    market_state_sha256=market_state_sha256,
                    affected_input_ids=affected_input_ids,
                    registered_input_ids=self.dependencies.input_ids,
                    decision_id=decision_id,
                    plan_sha256=plan.plan_sha256,
                    ledger_offset=ledger_offset,
                    gate=gate,
                )
                atomic_write_json(self.progress_path, durable_progress.to_dict())
                self._progress = durable_progress
                existing = self._verified_ledger_record_at_offset(ledger_offset)

            if existing is not None:
                verify_economic_goal_binding(
                    existing,
                    self.authority.contract,
                    self.authority.risk_policy,
                )
                if (
                    existing.context_hash != context_hash
                    or existing.payload.get("plan_sha256") != plan.plan_sha256
                    or existing.payload.get("market_state_sha256")
                    != market_state_sha256
                    or existing.payload.get("gate") != gate
                    or existing.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
                    != decision_id
                ):
                    raise DecisionLedgerIntegrityError(
                        "reserved live decision identity conflicts with durable evidence"
                    )
                duplicate = True
            else:
                self.decision_ledger.append_economic(record, self.authority)
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
                ledger_offset=ledger_offset,
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
            ledger_offset=None,
            gate=gate,
        )
        with WorkspaceEconomicLock(self.workspace):
            atomic_write_json(self.progress_path, pending.to_dict())
        self._progress = pending

    def _load_input_registry(self) -> tuple[_InputSpec, ...] | None:
        if not self.inputs_path.exists():
            return None
        try:
            raw = strict_json_loads(self.inputs_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise LiveDecisionProgressError(
                "cannot verify persisted live dependency registry"
            ) from exc
        if type(raw) is not dict or set(raw) != _INPUTS_KEYS:
            raise LiveDecisionProgressError(
                "live dependency registry must contain canonical fields"
            )
        if raw["schema"] != _INPUTS_SCHEMA or raw["schema_version"] != _INPUTS_VERSION:
            raise LiveDecisionProgressError("unsupported live dependency registry schema")
        if raw["loop_id"] != self.loop_id:
            raise LiveDecisionProgressError(
                "persisted live dependency registry belongs to a different loop_id"
            )
        values = raw["inputs"]
        if type(values) is not list:
            raise LiveDecisionProgressError("live dependency inputs must be a JSON array")
        specs = tuple(_InputSpec.from_dict(value) for value in values)
        if len({spec.input_id for spec in specs}) != len(specs):
            raise LiveDecisionProgressError(
                "live dependency registry contains duplicate input_id"
            )
        return specs

    def _persist_input_registry(
        self,
        *,
        expected_previous: tuple[_InputSpec, ...],
    ) -> None:
        candidate = tuple(self._input_specs.values())
        payload = {
            "schema": _INPUTS_SCHEMA,
            "schema_version": _INPUTS_VERSION,
            "loop_id": self.loop_id,
            "inputs": [spec.to_dict() for spec in candidate],
        }
        with WorkspaceEconomicLock(self.workspace):
            durable = self._load_input_registry() or ()
            if durable != expected_previous:
                raise LiveDecisionProgressError(
                    "live dependency registry changed concurrently"
                )
            atomic_write_json(self.inputs_path, payload)

    def _ledger_end_offset(self) -> int:
        path = self.decision_ledger.path
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                if size:
                    handle.seek(-1, 2)
                    if handle.read(1) != b"\n":
                        raise DecisionLedgerIntegrityError(
                            "Decision Ledger has an unterminated final record"
                        )
                return size
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger end offset is unreadable"
            ) from exc

    def _verified_ledger_record_at_offset(
        self,
        offset: int,
    ) -> DecisionRecord | None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise LiveDecisionProgressError("ledger_offset must be a non-negative integer")
        path = self.decision_ledger.path
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                if offset > size:
                    raise DecisionLedgerIntegrityError(
                        "reserved Decision Ledger offset is beyond durable bytes"
                    )
                if offset == size:
                    return None
                handle.seek(offset)
                line = handle.readline()
        except FileNotFoundError:
            if offset == 0:
                return None
            raise DecisionLedgerIntegrityError(
                "reserved Decision Ledger offset refers to a missing ledger"
            )
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "reserved Decision Ledger record is unreadable"
            ) from exc

        JsonlDecisionLedger._verify_bytes(line)
        envelope = json.loads(line.decode("utf-8"))
        record = JsonlDecisionLedger._validate_record(envelope["record"])
        return DecisionRecord(**record)

    def _verify_committed_progress_ledger_binding(
        self,
        progress: _Progress,
    ) -> None:
        if progress.phase != _PHASE_COMMITTED:
            raise LiveDecisionProgressError(
                "committed progress verification requires committed phase"
            )
        assert progress.decision_id is not None
        assert progress.plan_sha256 is not None
        assert progress.ledger_offset is not None

        context_payload = {
            "schema": "autosport.live_decision_context",
            "schema_version": 1,
            "loop_id": self.loop_id,
            "mode": self.mode.value,
            "gate": progress.gate,
            "market_state_sha256": progress.market_state_sha256,
            "plan_sha256": progress.plan_sha256,
        }
        expected_context_hash = _canonical_json_sha256(context_payload)
        expected_decision_id = f"live-{expected_context_hash}"
        if progress.decision_id != expected_decision_id:
            raise DecisionLedgerIntegrityError(
                "committed live progress decision identity is inconsistent"
            )

        existing = self._verified_ledger_record_at_offset(progress.ledger_offset)
        if existing is None:
            raise DecisionLedgerIntegrityError(
                "committed live decision is missing from Decision Ledger"
            )
        verify_economic_goal_binding(
            existing,
            self.authority.contract,
            self.authority.risk_policy,
        )
        if (
            existing.decision_id != progress.decision_id
            or existing.replay_run_id != f"live:{self.loop_id}"
            or existing.agent != self.AGENT_ID
            or existing.observed_ts != progress.decision_ts
            or existing.context_hash != expected_context_hash
            or existing.payload.get("schema")
            != "autosport.persistent_live_decision"
            or existing.payload.get("schema_version") != 1
            or existing.payload.get("loop_id") != self.loop_id
            or existing.payload.get("mode") != self.mode.value
            or existing.payload.get("gate") != progress.gate
            or existing.payload.get("market_state_sha256")
            != progress.market_state_sha256
            or existing.payload.get("affected_input_ids")
            != progress.affected_input_ids
            or existing.payload.get("plan_sha256") != progress.plan_sha256
            or existing.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
            != progress.decision_id
        ):
            raise DecisionLedgerIntegrityError(
                "committed live progress conflicts with Decision Ledger record"
            )

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
        specs = tuple(self._input_specs.values())
        payload = [
            event.to_dict()
            for event in self.mirror_updates.mirror.snapshot()
            if any(
                (spec.source_ids is None or event.source_id in spec.source_ids)
                and (spec.event_ids is None or event.event_id in spec.event_ids)
                and (spec.market_ids is None or event.market_id in spec.market_ids)
                and (
                    spec.selection_ids is None
                    or event.selection_id in spec.selection_ids
                )
                for spec in specs
            )
        ]
        return _canonical_json_sha256(payload)
