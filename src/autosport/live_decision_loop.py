from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from enum import Enum
from pathlib import Path
from time import monotonic
from typing import Callable, Protocol

from . import _paperbook_preload_authority_guard as _paperbook_authority
from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
    verify_economic_goal_binding,
)
from .economic_goal_provenance import provenance_for
from .event_lifecycle import CatalogCheckpoint, CatalogPage, ContinuousEventLifecycle
from .ingestion_health import IngestionPolicy, SourceHealthStore
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .live_observation import poll_open_market_store_once
from .market_mirror import MarketMirror, MirrorSnapshot
from .market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependency,
    FocusedMirrorDependencyIndex,
)
from .paper import PaperBook
from .paper_execution_adoption import (
    PaperExecutionAdoptionRuntime,
    PreparedPaperExecution,
)
from .portfolio_plan import (
    PortfolioDependencyGraph,
    PortfolioPlan,
    build_portfolio_plan,
)
from .providers import MarketProvider, ProviderUnavailableError
from .scientific_registry import ModelVersion, ScientificRegistry, StrategyVersion
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
    paper_execution_run_id: str | None = None
    paper_execution_attempt_ids: tuple[str, ...] = ()


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
    strategy_version_id: str

    def __call__(
        self,
        input_id: str,
        snapshot: MirrorSnapshot,
    ) -> tuple[object, ...]: ...


Clock = Callable[[], datetime]
ObservationRunner = Callable[[BoundedMirrorInvalidationBuffer], object]
CatalogPageFetcher = Callable[[CatalogCheckpoint | None], CatalogPage]
PostAppendHook = Callable[[], None]


_PROGRESS_SCHEMA = "autosport.live_decision_progress"
_PROGRESS_VERSION = 2
_PROGRESS_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "loop_id",
        "phase",
        "decision_ts",
        "market_state_sha256",
        "market_append_generation",
        "decision_context_sha256",
        "affected_input_ids",
        "registered_input_ids",
        "decision_id",
        "plan_sha256",
        "ledger_offset",
        "gate",
    }
)
_PROGRESS_KEYS_V1 = _PROGRESS_KEYS - {"market_append_generation"}
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
_INPUTS_VERSION = 2
_INPUTS_KEYS = frozenset({"schema", "schema_version", "loop_id", "inputs"})
_INPUT_SPEC_KEYS_V1 = frozenset(
    {"input_id", "source_ids", "event_ids", "market_ids", "selection_ids"}
)
_INPUT_SPEC_KEYS_V2 = frozenset(
    {"input_id", "source_ids", "sports", "event_ids", "market_ids", "selection_ids"}
)
_MARKET_FRONTIER_RETRY_LIMIT = 8


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


@dataclass(frozen=True, slots=True)
class LiveIntentProvenance:
    """Immutable live intent provenance resolved from canonical scientific memory."""

    strategy_version_id: str
    canonical_strategy_id: str
    source_sha256: str
    environment_sha256: str
    config_sha256: str
    strategy_record_sha256: str
    strategy_available_at: str
    model_version_id: str | None
    model_record_sha256: str | None
    model_available_at: str | None

    def __post_init__(self) -> None:
        _canonical_text("strategy_version_id", self.strategy_version_id)
        _canonical_text("canonical_strategy_id", self.canonical_strategy_id)
        _canonical_sha256("source_sha256", self.source_sha256)
        _canonical_sha256("environment_sha256", self.environment_sha256)
        _canonical_sha256("config_sha256", self.config_sha256)
        _canonical_sha256("strategy_record_sha256", self.strategy_record_sha256)
        _canonical_timestamp("strategy_available_at", self.strategy_available_at)
        if self.model_version_id is None:
            if self.model_record_sha256 is not None or self.model_available_at is not None:
                raise ValueError(
                    "model provenance requires model_version_id"
                )
        else:
            _canonical_text("model_version_id", self.model_version_id)
            if self.model_record_sha256 is None or self.model_available_at is None:
                raise ValueError(
                    "model_version_id requires complete model provenance"
                )
            _canonical_sha256("model_record_sha256", self.model_record_sha256)
            _canonical_timestamp("model_available_at", self.model_available_at)

    def assert_available_at(self, as_of: datetime) -> None:
        if not isinstance(as_of, datetime):
            raise TypeError("intent provenance as_of must be datetime")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("intent provenance as_of must be timezone-aware")
        cutoff = as_of.astimezone(timezone.utc)
        _, strategy_available = _canonical_timestamp(
            "strategy_available_at",
            self.strategy_available_at,
        )
        if strategy_available > cutoff:
            raise LiveDecisionProgressError(
                "registered live intent provenance was not causally available "
                "at decision time"
            )
        if self.model_available_at is not None:
            _, model_available = _canonical_timestamp(
                "model_available_at",
                self.model_available_at,
            )
            if model_available > cutoff:
                raise LiveDecisionProgressError(
                    "registered live intent provenance was not causally available "
                    "at decision time"
                )

    @classmethod
    def from_registry(
        cls,
        registry: ScientificRegistry,
        strategy_version_id: str,
        *,
        as_of: datetime,
    ) -> "LiveIntentProvenance":
        if not isinstance(registry, ScientificRegistry):
            raise TypeError("scientific_registry must be ScientificRegistry")
        wanted = _canonical_text("intent_strategy_version_id", strategy_version_id)
        strategy_entry = registry.get("StrategyVersion", wanted)
        if strategy_entry is None:
            raise LiveDecisionProgressError(
                "registered live intent StrategyVersion is missing"
            )
        try:
            strategy = StrategyVersion(**strategy_entry.payload)
        except (TypeError, ValueError) as exc:
            raise LiveDecisionProgressError(
                "registered live intent StrategyVersion is invalid"
            ) from exc
        if (
            strategy_entry.record_id != strategy.strategy_version_id
            or strategy.strategy_version_id != wanted
            or strategy_entry.available_at != strategy.created_at
        ):
            raise LiveDecisionProgressError(
                "registered live intent StrategyVersion identity is inconsistent"
            )

        model_record_sha256: str | None = None
        model_available_at: str | None = None
        if strategy.model_version_id is not None:
            model_entry = registry.get("ModelVersion", strategy.model_version_id)
            if model_entry is None:
                raise LiveDecisionProgressError(
                    "registered live intent StrategyVersion references missing ModelVersion"
                )
            try:
                model = ModelVersion(**model_entry.payload)
            except (TypeError, ValueError) as exc:
                raise LiveDecisionProgressError(
                    "registered live intent ModelVersion is invalid"
                ) from exc
            if (
                model_entry.record_id != model.model_version_id
                or model.model_version_id != strategy.model_version_id
                or model_entry.available_at != model.created_at
            ):
                raise LiveDecisionProgressError(
                    "registered live intent ModelVersion identity is inconsistent"
                )
            _, strategy_available = _canonical_timestamp(
                "StrategyVersion.available_at",
                strategy_entry.available_at,
            )
            _, model_available = _canonical_timestamp(
                "ModelVersion.available_at",
                model_entry.available_at,
            )
            if model_available > strategy_available:
                raise LiveDecisionProgressError(
                    "registered live intent ModelVersion was not available when "
                    "StrategyVersion became durable"
                )
            model_record_sha256 = model_entry.record_sha256
            model_available_at = model_entry.available_at

        provenance = cls(
            strategy_version_id=strategy.strategy_version_id,
            canonical_strategy_id=strategy.canonical_strategy_id,
            source_sha256=strategy.source_sha256,
            environment_sha256=strategy.environment_sha256,
            config_sha256=strategy.config_sha256,
            strategy_record_sha256=strategy_entry.record_sha256,
            strategy_available_at=strategy_entry.available_at,
            model_version_id=strategy.model_version_id,
            model_record_sha256=model_record_sha256,
            model_available_at=model_available_at,
        )
        provenance.assert_available_at(as_of)
        return provenance

    @property
    def provenance_sha256(self) -> str:
        return _canonical_json_sha256(
            {
                "schema": "autosport.live_intent_provenance",
                "schema_version": 1,
                "strategy_version_id": self.strategy_version_id,
                "canonical_strategy_id": self.canonical_strategy_id,
                "source_sha256": self.source_sha256,
                "environment_sha256": self.environment_sha256,
                "config_sha256": self.config_sha256,
                "strategy_record_sha256": self.strategy_record_sha256,
                "model_version_id": self.model_version_id,
                "model_record_sha256": self.model_record_sha256,
            }
        )


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
    sports: tuple[str, ...] | None
    event_ids: tuple[str, ...] | None
    market_ids: tuple[str, ...] | None
    selection_ids: tuple[str, ...] | None

    def __post_init__(self) -> None:
        FocusedMirrorDependencyIndex._input_id(self.input_id)
        for name in ("source_ids", "sports", "event_ids", "market_ids", "selection_ids"):
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
            sports=(
                None
                if dependency.sports is None
                else tuple(sorted(dependency.sports))
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
            "sports": None if self.sports is None else list(self.sports),
            "event_ids": None if self.event_ids is None else list(self.event_ids),
            "market_ids": None if self.market_ids is None else list(self.market_ids),
            "selection_ids": (
                None if self.selection_ids is None else list(self.selection_ids)
            ),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "_InputSpec":
        if type(raw) is not dict or frozenset(raw) not in {
            _INPUT_SPEC_KEYS_V1,
            _INPUT_SPEC_KEYS_V2,
        }:
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
                sports=None if "sports" not in raw else selector("sports"),
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
    market_append_generation: int | None
    decision_context_sha256: str
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
        if self.market_append_generation is not None and (
            type(self.market_append_generation) is not int
            or self.market_append_generation < 0
        ):
            raise LiveDecisionProgressError(
                "market_append_generation must be a non-negative int or null"
            )
        _canonical_sha256("decision_context_sha256", self.decision_context_sha256)
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
            if self.decision_id is not None or self.plan_sha256 is not None:
                raise LiveDecisionProgressError(
                    "pending live progress cannot claim append identity"
                )
            # New PENDING cursors freeze the Decision Ledger byte frontier at
            # publication time. Legacy v1 cursors may still carry null here.
            if self.ledger_offset is not None and (
                isinstance(self.ledger_offset, bool)
                or not isinstance(self.ledger_offset, int)
                or self.ledger_offset < 0
            ):
                raise LiveDecisionProgressError(
                    "pending live progress ledger frontier must be non-negative"
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
            "market_append_generation": self.market_append_generation,
            "decision_context_sha256": self.decision_context_sha256,
            "affected_input_ids": list(self.affected_input_ids),
            "registered_input_ids": list(self.registered_input_ids),
            "decision_id": self.decision_id,
            "plan_sha256": self.plan_sha256,
            "ledger_offset": self.ledger_offset,
            "gate": self.gate,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "_Progress":
        if type(raw) is not dict:
            raise LiveDecisionProgressError(
                "live decision progress must contain canonical fields"
            )
        schema_version = raw.get("schema_version")
        expected_keys = (
            _PROGRESS_KEYS
            if schema_version == _PROGRESS_VERSION
            else _PROGRESS_KEYS_V1
            if schema_version == 1
            else None
        )
        if expected_keys is None or set(raw) != expected_keys:
            raise LiveDecisionProgressError(
                "live decision progress must contain canonical fields"
            )
        if raw["schema"] != _PROGRESS_SCHEMA:
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
                market_append_generation=(
                    None
                    if schema_version == 1
                    else raw["market_append_generation"]
                ),
                decision_context_sha256=raw["decision_context_sha256"],
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
    PRE_ACTION_BOOK_FILE_NAME = "live_decision_pre_action_book.json"
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
        scientific_registry: ScientificRegistry,
        provider: MarketProvider | None = None,
        decision_ledger: JsonlDecisionLedger | None = None,
        paper_execution: PaperExecutionAdoptionRuntime | None = None,
        ingestion_policy: IngestionPolicy | None = None,
        max_quote_age: timedelta | None = None,
        bounds: LiveLoopBounds | None = None,
        clock: Clock | None = None,
        observation_runner: ObservationRunner | None = None,
        post_append_hook: PostAppendHook | None = None,
        catalog_lifecycle: ContinuousEventLifecycle | None = None,
        catalog_fetch_page: CatalogPageFetcher | None = None,
        catalog_source_id: str | None = None,
        catalog_required_history: timedelta = timedelta(0),
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
        factory_strategy_version_id = getattr(
            intent_factory,
            "strategy_version_id",
            None,
        )
        if type(factory_strategy_version_id) is not str:
            raise TypeError(
                "intent_factory must expose canonical strategy_version_id"
            )
        resolved_clock = clock or (lambda: datetime.now(timezone.utc))
        provenance_as_of = _require_utc_clock(resolved_clock)
        intent_provenance = LiveIntentProvenance.from_registry(
            scientific_registry,
            factory_strategy_version_id,
            as_of=provenance_as_of,
        )
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
        self.intent_provenance = intent_provenance
        self.provider = provider
        self.decision_ledger = decision_ledger or JsonlDecisionLedger(
            self.workspace / "decisions.jsonl"
        )
        if paper_execution is not None:
            if not isinstance(paper_execution, PaperExecutionAdoptionRuntime):
                raise TypeError(
                    "paper_execution must be PaperExecutionAdoptionRuntime or None"
                )
            if paper_execution.book is not book:
                raise ValueError(
                    "paper_execution must materialize into the live loop PaperBook"
                )
            canonical_book_path = self.workspace / "paper_book.json"
            if paper_execution.paper_book_path != canonical_book_path:
                raise ValueError(
                    "paper_execution must persist the canonical live workspace PaperBook"
                )
        self.paper_execution = paper_execution
        self.ingestion_policy = ingestion_policy
        self.max_quote_age = max_quote_age
        self.bounds = bounds or LiveLoopBounds()
        self.clock = resolved_clock
        self._last_clock_time = provenance_as_of
        self.post_append_hook = post_append_hook
        if catalog_lifecycle is not None and not isinstance(
            catalog_lifecycle, ContinuousEventLifecycle
        ):
            raise TypeError("catalog_lifecycle must be ContinuousEventLifecycle or None")
        if catalog_fetch_page is not None and not callable(catalog_fetch_page):
            raise TypeError("catalog_fetch_page must be callable or None")
        if catalog_source_id is not None:
            catalog_source_id = _canonical_text("catalog_source_id", catalog_source_id)
        if catalog_required_history < timedelta(0):
            raise ValueError("catalog_required_history must be non-negative")
        if (catalog_lifecycle is None) != (catalog_fetch_page is None):
            raise ValueError(
                "catalog_lifecycle and catalog_fetch_page must be supplied together"
            )
        if catalog_fetch_page is not None and catalog_source_id is None:
            raise ValueError(
                "catalog_source_id is required when catalog lifecycle refresh is enabled"
            )
        self.catalog_lifecycle = catalog_lifecycle
        self.catalog_fetch_page = catalog_fetch_page
        self.catalog_source_id = catalog_source_id
        self.catalog_required_history = catalog_required_history
        self._default_market_store: SQLiteMarketStore | None = None
        self._default_health_store: SourceHealthStore | None = None
        self._default_market_change_token: int | None = None

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
                sports=spec.sports,
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
                store = self._default_market_store
                health_store = self._default_health_store
                opened_here = store is None
                if opened_here:
                    store = SQLiteMarketStore(self.workspace / "market.db")
                    try:
                        health_store = SourceHealthStore(
                            self.workspace / "source_health.json"
                        )
                    except BaseException:
                        store.close()
                        raise
                assert store is not None
                assert health_store is not None

                try:
                    self._reconcile_default_market_changes(
                        store,
                        updates,
                        force=opened_here,
                    )
                except BaseException:
                    if opened_here:
                        store.close()
                    raise

                if opened_here:
                    self._default_market_store = store
                    self._default_health_store = health_store

                try:
                    result = poll_open_market_store_once(
                        store,
                        health_store,
                        provider,
                        mirror_updates=updates,
                        max_items=self.bounds.observation_max_items,
                        policy=self.ingestion_policy,
                    )
                except ProviderUnavailableError:
                    # A peer may have committed market truth while provider I/O was
                    # failing. Reconcile it before the caller persists a ZERO
                    # provider-gap decision against this observation boundary.
                    self._reconcile_default_market_changes(store, updates)
                    raise

                # Catch peer commits that landed while provider I/O was in flight.
                # Same-connection appends are already delivered synchronously by the
                # local MarketEventBus and do not advance SQLite data_version here.
                self._reconcile_default_market_changes(store, updates)
                return result

            self._observe = _default_observer
        else:
            self._observe = observation_runner

        self.progress_path = self.workspace / self.PROGRESS_FILE_NAME
        self.pre_action_book_path = self.workspace / self.PRE_ACTION_BOOK_FILE_NAME
        self.control_path = self.workspace / self.CONTROL_FILE_NAME
        self._progress = self._load_progress()
        if self._progress is not None and self._progress.loop_id != self.loop_id:
            raise LiveDecisionProgressError(
                "persisted live progress belongs to a different loop_id"
            )
        if self._progress is not None:
            _, durable_decision_time = _canonical_timestamp(
                "persisted decision_ts",
                self._progress.decision_ts,
            )
            if durable_decision_time > self._last_clock_time:
                self._last_clock_time = durable_decision_time
        if self.decision_ledger.path.exists():
            with WorkspaceEconomicLock(self.workspace):
                self.decision_ledger.verify_integrity()
                if self._progress is None:
                    live_run_id = f"live:{self.loop_id}"
                    if self._verified_latest_ledger_record(
                        replay_run_id=live_run_id,
                    ) is not None:
                        raise LiveDecisionProgressError(
                            "durable live decision history exists but progress is missing"
                        )
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
        if (
            self._progress is not None
            and self._progress.phase in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
            and self._progress.registered_input_ids != self.dependencies.input_ids
        ):
            raise LiveDecisionProgressError(
                "unfinished live decision requires exact durable dependency registry"
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
        self._input_market_sha256: dict[str, str] = {}
        self._pending_affected: dict[str, None] = {}
        self._needs_cache_rebuild = True
        self._freshness_deadlines: dict[str, datetime | None] = {}
        self._freshness_generations: dict[str, int] = {}
        self._freshness_heap: list[tuple[datetime, str, int]] = []
        self._availability_deadlines: dict[str, datetime | None] = {}
        self._availability_generations: dict[str, int] = {}
        self._availability_heap: list[tuple[datetime, str, int]] = []
        self._decision_market_frontier_as_of: datetime | None = None
        self._decision_market_append_generation: int | None = None
        self._decision_market_history: tuple[tuple[MarketEvent, int], ...] | None = None
        self._decision_market_history_frozen = False

    def close(self) -> None:
        """Release the optional long-lived default market-store connection."""
        store = self._default_market_store
        self._default_market_store = None
        self._default_health_store = None
        self._default_market_change_token = None
        self._decision_market_frontier_as_of = None
        self._decision_market_append_generation = None
        self._decision_market_history = None
        self._decision_market_history_frozen = False
        if store is not None:
            store.close()

    def _reconcile_default_market_changes(
        self,
        store: SQLiteMarketStore,
        updates: BoundedMirrorInvalidationBuffer,
        *,
        force: bool = False,
    ) -> int:
        """Reconcile peer-process commits through independently proven market truth."""

        if not isinstance(store, SQLiteMarketStore):
            raise TypeError("store must be a SQLiteMarketStore")
        if not isinstance(updates, BoundedMirrorInvalidationBuffer):
            raise TypeError("updates must be a BoundedMirrorInvalidationBuffer")
        if type(force) is not bool:
            raise TypeError("force must be a bool")

        # MarketEventBus delivery is process-local. SQLite data_version is only a
        # cheap cross-connection invalidation hint; market values still enter the
        # mirror exclusively through the independently proven current projection.
        change_token = store.external_change_token()
        if not force and self._default_market_change_token == change_token:
            return change_token
        for (
            persisted_event,
            append_generation,
        ) in store.current_by_source_with_append_generation().values():
            updates.reconcile_persisted(
                persisted_event,
                append_generation=append_generation,
            )
        self._default_market_change_token = change_token
        return change_token

    def _decision_refresh_may_need_history(self, as_of: datetime) -> bool:
        """Return whether this cycle can reach focused snapshot materialization."""

        if (
            self.mirror_updates.pending_count
            or self.mirror_updates.full_refresh_required
            or self._pending_affected
            or self._needs_cache_rebuild
        ):
            return True
        return any(
            deadline is not None and deadline <= as_of
            for deadline in (
                *self._freshness_deadlines.values(),
                *self._availability_deadlines.values(),
            )
        )

    def _sample_decision_market_frontier(self) -> datetime:
        """Choose a cutoff and freeze any exceptional history it may consume."""

        self._decision_market_frontier_as_of = None
        self._decision_market_append_generation = None
        self._decision_market_history = None
        self._decision_market_history_frozen = False

        store = self._default_market_store
        if store is None:
            return self._sample_clock()

        # The token is sampled before trusted projection reconciliation and again
        # after the candidate decision clock. If a peer commits anywhere across that
        # interval, discard the candidate cutoff, reconcile the newly durable truth,
        # and sample again. When the latest-only mirror hides a causally visible
        # predecessor, freeze the already-proven append history inside this same token
        # interval. A later fallback must consume this snapshot rather than re-open the
        # database after the decision cutoff.
        for _ in range(_MARKET_FRONTIER_RETRY_LIMIT):
            expected_token = self._reconcile_default_market_changes(
                store,
                self.mirror_updates,
            )
            decision_time = self._sample_clock()
            append_generation = store.committed_append_generation_head()
            frozen_history: tuple[tuple[MarketEvent, int], ...] | None = None
            if self._decision_refresh_may_need_history(decision_time) and any(
                self.dependencies.requires_current_history_fallback(
                    input_id,
                    as_of=decision_time,
                )
                for input_id in self.dependencies.input_ids
            ):
                frozen_history = tuple(store.events_with_append_generation())

            if store.external_change_token() == expected_token:
                self._decision_market_frontier_as_of = decision_time
                self._decision_market_append_generation = append_generation
                self._decision_market_history = frozen_history
                self._decision_market_history_frozen = True
                return decision_time

        raise LiveDecisionProgressError(
            "cross-process market truth changed continuously across decision cutoff"
        )

    def __enter__(self) -> "PersistentLiveDecisionLoop":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

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
        sports: str | tuple[str, ...] | None = None,
        event_ids: str | tuple[str, ...] | None = None,
        market_ids: str | tuple[str, ...] | None = None,
        selection_ids: str | tuple[str, ...] | None = None,
    ) -> None:
        normalized_id = FocusedMirrorDependencyIndex._input_id(input_id)
        candidate = _InputSpec(
            input_id=normalized_id,
            source_ids=_selector_tuple(source_ids, name="source_ids"),
            sports=_selector_tuple(sports, name="sports"),
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
            sports=candidate.sports,
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

    def unregister_input(self, input_id: str) -> bool:
        normalized_id = FocusedMirrorDependencyIndex._input_id(input_id)
        existing = self._input_specs.get(normalized_id)
        if existing is None:
            return False
        previous_specs = tuple(self._input_specs.values())
        if not self.dependencies.unregister(normalized_id):
            raise LiveDecisionProgressError(
                "live dependency registry is inconsistent during retirement"
            )
        self._input_specs.pop(normalized_id)
        try:
            self._persist_input_registry(expected_previous=previous_specs)
        except BaseException:
            # Rebuild the exact pre-mutation ordering rather than merely appending
            # the removed dependency back to the end. Registry order participates
            # in durable progress identity, so a failed publication must be a true
            # in-memory no-op as well as a durable no-op.
            for current_id in self.dependencies.input_ids:
                if not self.dependencies.unregister(current_id):
                    raise LiveDecisionProgressError(
                        "live dependency rollback could not clear current registry"
                    )
            self._input_specs.clear()
            for spec in previous_specs:
                restored_dependency = self.dependencies.register(
                    spec.input_id,
                    source_ids=spec.source_ids,
                    sports=spec.sports,
                    event_ids=spec.event_ids,
                    market_ids=spec.market_ids,
                    selection_ids=spec.selection_ids,
                )
                if _InputSpec.from_dependency(restored_dependency) != spec:
                    raise LiveDecisionProgressError(
                        "live dependency rollback changed canonical selectors"
                    )
                self._input_specs[spec.input_id] = spec
            raise
        self._pending_affected.pop(normalized_id, None)
        self._intent_cache.pop(normalized_id, None)
        self._input_market_sha256.pop(normalized_id, None)
        self._freshness_deadlines.pop(normalized_id, None)
        self._freshness_generations[normalized_id] = (
            self._freshness_generations.get(normalized_id, 0) + 1
        )
        self._availability_deadlines.pop(normalized_id, None)
        self._availability_generations[normalized_id] = (
            self._availability_generations.get(normalized_id, 0) + 1
        )
        return True

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

    def _refresh_cycle_authorities(self) -> None:
        """Fence stale loop instances before catalog/provider observation."""

        with WorkspaceEconomicLock(self.workspace):
            durable_progress = self._load_progress()
            if durable_progress != self._progress:
                raise LiveDecisionProgressError(
                    "live decision progress changed concurrently before cycle"
                )

            durable_input_specs = self._load_input_registry() or ()
            if durable_input_specs != tuple(self._input_specs.values()):
                raise LiveDecisionProgressError(
                    "live dependency registry changed concurrently before cycle"
                )

            durable_control = self._load_control()
            if durable_control is None:
                durable_control = _Control(self.loop_id, LiveControlState.RUNNING)
            elif durable_control.loop_id != self.loop_id:
                raise LiveDecisionProgressError(
                    "persisted live control belongs to a different loop_id"
                )

        self._control = durable_control

    def _sample_clock(self) -> datetime:
        """Return UTC wall time without allowing causal decision chronology to regress."""

        now = _require_utc_clock(self.clock)
        if now < self._last_clock_time:
            raise LiveDecisionProgressError(
                "live decision clock moved backwards across causal chronology"
            )
        self._last_clock_time = now
        return now

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
        self._refresh_cycle_authorities()

        # PENDING/APPEND_PENDING is an already-started durable transaction. Finish
        # or fail closed on that exact identity before honoring a later PAUSE/STOP;
        # otherwise an operator control written after publication can strand an
        # economic decision forever in an unverifiable half-state.
        if (
            self._progress is not None
            and self._progress.phase in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
        ):
            return self._recover_unfinished_progress()

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

        catalog_now = self._sample_clock()
        try:
            if self.catalog_lifecycle is not None:
                self._refresh_catalog_lifecycle(catalog_now)
                # Catalog refresh can perform provider I/O and may publish durable
                # dependency changes through register/retire callbacks.  Re-fence
                # every workspace authority before starting the main market poll:
                # a peer STOP/PAUSE, decision-progress advance, or registry mutation
                # that happened while catalog I/O was in flight must win at this
                # safe boundary rather than allowing one more stale provider call.
                self._refresh_cycle_authorities()
                if self.stopped:
                    return LiveCycleResult(
                        LiveCycleStatus.STOPPED,
                        detail=(
                            "durable STOP became active during catalog refresh; "
                            "market provider was not polled"
                        ),
                    )
                if self.paused:
                    return LiveCycleResult(
                        LiveCycleStatus.PAUSED,
                        detail=(
                            "durable PAUSE became active during catalog refresh; "
                            "market provider was not polled"
                        ),
                    )
            self._observe(self.mirror_updates)
            # Provider observation is another external-I/O boundary.  Re-resolve
            # durable workspace authorities before draining invalidations or doing
            # any decision work so a peer mutation that happened while the poll was
            # in flight cannot flow through stale process-local state.
            self._refresh_cycle_authorities()
            if self.stopped:
                return LiveCycleResult(
                    LiveCycleStatus.STOPPED,
                    detail=(
                        "durable STOP became active during provider observation; "
                        "no economic decision was published"
                    ),
                )
            if self.paused:
                return LiveCycleResult(
                    LiveCycleStatus.PAUSED,
                    detail=(
                        "durable PAUSE became active during provider observation; "
                        "no economic decision was published"
                    ),
                )
        except ProviderUnavailableError as exc:
            self._needs_cache_rebuild = True
            # A provider failure does not outrank a concurrent operator control or
            # peer decision/registry publication.  Fence those authorities before
            # turning the failure into durable provider-gap economic evidence.
            self._refresh_cycle_authorities()
            if self.stopped:
                return LiveCycleResult(
                    LiveCycleStatus.STOPPED,
                    detail=(
                        "durable STOP became active during failed provider observation; "
                        "provider gap was not published"
                    ),
                )
            if self.paused:
                return LiveCycleResult(
                    LiveCycleStatus.PAUSED,
                    detail=(
                        "durable PAUSE became active during failed provider observation; "
                        "provider gap was not published"
                    ),
                )
            return self._persist_provider_gap(
                self._sample_decision_market_frontier(),
                exc,
            )

        now = self._sample_decision_market_frontier()
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

        freshness_expired_inputs = self._expire_freshness_inputs(now)
        availability_reached_inputs = self._activate_available_inputs(now)
        for input_id in (*freshness_expired_inputs, *availability_reached_inputs):
            self._pending_affected[input_id] = None
        freshness_expired = bool(
            freshness_expired_inputs or availability_reached_inputs
        )

        registered_input_ids = self.dependencies.input_ids
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
        snapshots = self._capture_input_views(refresh_input_ids, decision_time)
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
            self._refresh_intents_from_snapshots(snapshots)
            self._pending_affected.clear()
            self._needs_cache_rebuild = False
            return LiveCycleResult(
                LiveCycleStatus.NO_CHANGE,
                detail=(
                    "clean restart rebuilt deterministic intent cache from the "
                    "same canonical decision-visible market state"
                ),
            )

        decision_ts = now.isoformat()
        self._write_pending(
            decision_ts=decision_ts,
            market_state_sha256=current_market_sha,
            affected_input_ids=affected,
            gate=_GATE_NORMAL,
        )
        self._refresh_intents_from_snapshots(snapshots)

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
            intents=intents,
            market_state_sha256=current_market_sha,
            affected_input_ids=affected,
            gate=_GATE_NORMAL,
        )
        self._pending_affected.clear()
        self._needs_cache_rebuild = False
        return result

    def _refresh_catalog_lifecycle(self, now: datetime) -> tuple[str, ...]:
        lifecycle = self.catalog_lifecycle
        fetch_page = self.catalog_fetch_page
        source_id = self.catalog_source_id
        if lifecycle is None or fetch_page is None or source_id is None:
            return ()
        store = SQLiteMarketStore(self.workspace / "market.db")
        try:
            return lifecycle.refresh_and_register(
                fetch_page,
                store,
                source_id=source_id,
                discovered_at=now.isoformat(),
                required_history=self.catalog_required_history,
                register_input=self.register_input,
                retire_input=self.unregister_input,
            )
        finally:
            store.close()

    def _verify_intent_factory_provenance(self) -> None:
        factory_strategy_version_id = getattr(
            self.intent_factory,
            "strategy_version_id",
            None,
        )
        try:
            factory_strategy_version_id = _canonical_text(
                "intent_factory.strategy_version_id",
                factory_strategy_version_id,
            )
        except ValueError as exc:
            raise LiveDecisionProgressError(
                "live intent factory lost canonical strategy-version provenance"
            ) from exc
        if factory_strategy_version_id != self.intent_provenance.strategy_version_id:
            raise LiveDecisionProgressError(
                "live intent factory strategy-version provenance changed"
            )

    @staticmethod
    def _same_book_state(left: PaperBook, right: PaperBook) -> bool:
        return (
            left.initial_bankroll == right.initial_bankroll
            and left.balance == right.balance
            and left.tickets == right.tickets
            and left._lifecycle == right._lifecycle
            and left._settlement_times == right._settlement_times
        )

    def _decision_context_sha256_for_book(self, book: PaperBook) -> str:
        self._verify_intent_factory_provenance()
        if not isinstance(book, PaperBook):
            raise TypeError("book must be PaperBook")
        book_state_sha256 = self.authority.risk_policy.risk_of_ruin_portfolio_sha256(
            book
        )
        if book_state_sha256 is None:
            raise LiveDecisionProgressError(
                "cannot derive canonical PaperBook decision context"
            )
        provenance = self.intent_provenance
        return _canonical_json_sha256(
            {
                "schema": "autosport.live_decision_runtime_context",
                "schema_version": 2,
                "mode": self.mode.value,
                "intent_strategy_version_id": provenance.strategy_version_id,
                "intent_model_version_id": provenance.model_version_id,
                "intent_provenance_sha256": provenance.provenance_sha256,
                "economic_goal_contract_sha256": provenance_for(
                    self.authority.contract
                ).contract_sha256,
                "risk_policy_sha256": self.authority.risk_policy.provenance_sha256,
                "book_state_sha256": book_state_sha256,
                "max_quote_age_seconds": str(
                    _timedelta_decimal_seconds(self.max_quote_age)
                ),
            }
        )

    def _decision_context_sha256(self) -> str:
        return self._decision_context_sha256_for_book(self.book)

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
        self.intent_provenance.assert_available_at(decision_time)

        # PENDING publication persists the exact pre-action PaperBook separately
        # from the crash cursor. That snapshot remains the decision/risk authority
        # even when #623 has already durably materialized accepted exposure into
        # the canonical PaperBook before progress reaches COMMITTED.
        if self.pre_action_book_path.exists():
            try:
                pre_action_book = PaperBook.load(self.pre_action_book_path)
            except (OSError, TypeError, ValueError) as exc:
                raise LiveDecisionProgressError(
                    "unfinished live decision pre-action PaperBook is unreadable"
                ) from exc
        else:
            # Compatibility with progress written before the snapshot seam:
            # recovery is permitted only while the current book still proves the
            # original decision context.
            pre_action_book = self.book

        if (
            progress.decision_context_sha256
            != self._decision_context_sha256_for_book(pre_action_book)
        ):
            raise LiveDecisionProgressError(
                "unfinished live decision runtime context changed across restart"
            )
        if (
            progress.phase == _PHASE_PENDING
            and not self._same_book_state(self.book, pre_action_book)
        ):
            raise LiveDecisionProgressError(
                "unfinished live decision runtime context changed: "
                "PaperBook changed before durable decision"
            )

        if progress.gate == _GATE_NORMAL:
            self._refresh_intents_from_replay(
                progress.registered_input_ids,
                decision_time,
                expected_market_state_sha256=progress.market_state_sha256,
                max_append_generation=progress.market_append_generation,
            )
            intents = self._all_cached_intents()
        else:
            intents = ()

        # Once the exact economic DecisionRecord is durable, it is the immutable
        # pre-action plan authority. In particular, an accepted #623 attempt may
        # already have published the canonical PaperBook while progress is still
        # APPEND_PENDING; never re-size/re-decide economics from that post-action
        # state.
        durable_record = None
        if (
            progress.phase == _PHASE_APPEND_PENDING
            and progress.ledger_offset is not None
        ):
            durable_record = self._verified_ledger_record_at_offset(
                progress.ledger_offset
            )

        if durable_record is not None:
            if progress.decision_id is None or progress.plan_sha256 is None:
                raise LiveDecisionProgressError(
                    "append-pending recovery lacks reserved decision identity"
                )
            verify_economic_goal_binding(
                durable_record,
                self.authority.contract,
                self.authority.risk_policy,
            )
            if (
                durable_record.decision_id != progress.decision_id
                or durable_record.payload.get("plan_sha256")
                != progress.plan_sha256
                or durable_record.payload.get("market_state_sha256")
                != progress.market_state_sha256
                or durable_record.payload.get("market_append_generation")
                != progress.market_append_generation
                or durable_record.payload.get("decision_context_sha256")
                != progress.decision_context_sha256
                or durable_record.payload.get("gate") != progress.gate
                or durable_record.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
                != progress.decision_id
            ):
                raise LiveDecisionProgressError(
                    "append-pending durable decision conflicts with progress"
                )
            try:
                # DecisionRecord freezes mappings/lists after verification. Re-enter
                # the canonical JSON parser through its detached representation
                # rather than weakening PortfolioPlan.from_dict to trust mappings.
                detached_payload = durable_record.to_dict()["payload"]
                plan = PortfolioPlan.from_dict(detached_payload.get("plan"))
            except (KeyError, TypeError, ValueError) as exc:
                raise LiveDecisionProgressError(
                    "append-pending durable PortfolioPlan is invalid"
                ) from exc
            if plan.plan_sha256 != progress.plan_sha256:
                raise LiveDecisionProgressError(
                    "append-pending durable PortfolioPlan identity changed"
                )
            latest_live = self._verified_latest_ledger_record(
                replay_run_id=f"live:{self.loop_id}",
            )
            if (
                latest_live is None
                or latest_live[1].decision_id != durable_record.decision_id
            ):
                raise LiveDecisionProgressError(
                    "append-pending live progress is not the latest durable live decision"
                )
            if (
                tuple(getattr(intent, "intent_id", None) for intent in intents)
                != plan.intent_ids
                or tuple(
                    getattr(intent, "intent_sha256", None) for intent in intents
                )
                != plan.intent_sha256s
                or tuple(
                    getattr(
                        getattr(intent, "opportunity_class", None),
                        "value",
                        None,
                    )
                    for intent in intents
                )
                != plan.opportunity_classes
            ):
                raise LiveDecisionProgressError(
                    "append-pending replayed intents conflict with durable PortfolioPlan"
                )
        else:
            graph = (
                None
                if not intents
                else PortfolioDependencyGraph.for_inputs(pre_action_book, intents)
            )
            plan = build_portfolio_plan(
                pre_action_book,
                intents,
                self.authority.risk_policy,
                decision_ts,
                dependency_graph=graph,
                market_outcome_authorities=(),
            )

        if progress.phase == _PHASE_PENDING:
            if (
                progress.ledger_offset is not None
                and self._ledger_end_offset() < progress.ledger_offset
            ):
                raise DecisionLedgerIntegrityError(
                    "pending live decision ledger frontier was truncated"
                )
            prospective_decision_id = self._decision_identity(
                plan=plan,
                market_state_sha256=progress.market_state_sha256,
                gate=progress.gate,
                decision_context_sha256=progress.decision_context_sha256,
            )[1]
            latest_live = self._verified_latest_ledger_record(
                replay_run_id=f"live:{self.loop_id}",
            )
            if latest_live is not None:
                latest_offset, latest_record = latest_live
                if (
                    progress.ledger_offset is not None
                    and latest_offset >= progress.ledger_offset
                ):
                    raise LiveDecisionProgressError(
                        "pending live progress was superseded after publication"
                    )
                _, latest_time = _canonical_timestamp(
                    "latest durable live decision observed_ts",
                    latest_record.observed_ts,
                )
                if (
                    latest_record.decision_id == prospective_decision_id
                    or latest_time > decision_time
                ):
                    raise LiveDecisionProgressError(
                        "pending live progress predates an already durable live decision"
                    )

        result = self._persist_plan(
            plan=plan,
            intents=intents,
            market_state_sha256=progress.market_state_sha256,
            affected_input_ids=progress.affected_input_ids,
            gate=progress.gate,
            detail=(
                "recovered unfinished durable live decision before provider polling"
            ),
            decision_context_sha256_override=progress.decision_context_sha256,
        )
        self._pending_affected.clear()
        self._needs_cache_rebuild = True
        self._input_market_sha256.clear()
        self._freshness_deadlines.clear()
        self._freshness_generations.clear()
        self._freshness_heap.clear()
        self._availability_deadlines.clear()
        self._availability_generations.clear()
        self._availability_heap.clear()
        return result

    def _refresh_intents_from_replay(
        self,
        input_ids: tuple[str, ...],
        as_of: datetime,
        *,
        expected_market_state_sha256: str,
        max_append_generation: int | None,
    ) -> None:
        _canonical_sha256(
            "expected replay market_state_sha256",
            expected_market_state_sha256,
        )
        store = SQLiteMarketStore(self.workspace / "market.db")
        try:
            if max_append_generation is None:
                # Legacy progress did not persist a market-generation frontier.
                snapshot = MarketMirror.replay_view_from_store(
                    store,
                    as_of=as_of,
                    max_age=self.max_quote_age,
                )
            else:
                boundary, age_limit = MarketMirror._decision_boundary(
                    as_of=as_of,
                    max_age=self.max_quote_age,
                )
                snapshot = MarketMirror._decision_view_from_proven_history(
                    store.events_at_committed_append_boundary(
                        max_append_generation
                    ),
                    boundary=boundary,
                    max_age=age_limit,
                    source_ids=None,
                    sports=None,
                    event_ids=None,
                    market_ids=None,
                    selection_ids=None,
                )
        finally:
            store.close()

        replay_market_sha256 = self._market_state_sha256_for_events(snapshot.events)
        if replay_market_sha256 != expected_market_state_sha256:
            raise LiveDecisionProgressError(
                "unfinished live decision replayed market state changed across restart"
            )

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
                        and (spec.sports is None or event.sport in spec.sports)
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
            self._intent_cache[input_id] = self._validated_intents(produced)

    def _validated_intents(self, produced: object) -> tuple[object, ...]:
        from .portfolio_plan import OpportunityIntent

        self._verify_intent_factory_provenance()
        if type(produced) is not tuple:
            raise TypeError("intent_factory must return a tuple")
        if any(not isinstance(intent, OpportunityIntent) for intent in produced):
            raise TypeError(
                "intent_factory must return only canonical OpportunityIntent values"
            )
        provenance = self.intent_provenance
        for intent in produced:
            if intent.strategy_id != provenance.strategy_version_id:
                raise LiveDecisionProgressError(
                    "live intent strategy identity does not match registered StrategyVersion"
                )
            if intent.config_sha256 != provenance.config_sha256:
                raise LiveDecisionProgressError(
                    "live intent config identity does not match registered StrategyVersion"
                )
            if intent.model_id != provenance.model_version_id:
                raise LiveDecisionProgressError(
                    "live intent model identity does not match registered StrategyVersion"
                )
        return produced

    def _capture_input_views(
        self,
        input_ids: tuple[str, ...],
        as_of: datetime,
        *,
        incremental: bool = True,
    ) -> dict[str, MirrorSnapshot]:
        snapshots: dict[str, MirrorSnapshot] = {}
        reader = (
            self.dependencies.incremental_decision_view
            if incremental
            else self.dependencies.decision_view
        )
        history_store: SQLiteMarketStore | None = None
        history_events: tuple[tuple[MarketEvent, int], ...] | None = None
        owns_history_store = False
        try:
            for input_id in input_ids:
                history_fallback = self.dependencies.requires_current_history_fallback(
                    input_id,
                    as_of=as_of,
                )
                if history_fallback:
                    if (
                        self._decision_market_history_frozen
                        and self._decision_market_frontier_as_of == as_of
                    ):
                        if self._decision_market_history is None:
                            raise LiveDecisionProgressError(
                                "decision frontier did not freeze required market history"
                            )
                        history_events = self._decision_market_history
                    else:
                        if history_store is None:
                            history_store = self._default_market_store
                            if history_store is None:
                                history_store = SQLiteMarketStore(
                                    self.workspace / "market.db"
                                )
                                owns_history_store = True
                        if history_events is None:
                            history_events = tuple(
                                history_store.events_with_append_generation()
                            )
                    (
                        snapshot,
                        next_history_availability,
                    ) = self.dependencies.decision_state_from_proven_history(
                        input_id,
                        history_events,
                        as_of=as_of,
                        max_age=self.max_quote_age,
                    )
                else:
                    snapshot = reader(
                        input_id,
                        as_of=as_of,
                        max_age=self.max_quote_age,
                    )
                    next_history_availability = None
                snapshots[input_id] = snapshot
                self._input_market_sha256[input_id] = _canonical_json_sha256(
                    [event.to_dict() for event in snapshot.events]
                )
                self._record_freshness_deadline(input_id, snapshot)
                if history_fallback:
                    self._set_availability_deadline(
                        input_id,
                        next_history_availability,
                    )
                else:
                    self._record_availability_deadline(input_id, as_of)
        finally:
            if owns_history_store and history_store is not None:
                history_store.close()
        return snapshots

    def _refresh_intents_from_snapshots(
        self,
        snapshots: dict[str, MirrorSnapshot],
    ) -> None:
        for input_id, snapshot in snapshots.items():
            produced = self.intent_factory(input_id, snapshot)
            self._intent_cache[input_id] = self._validated_intents(produced)

    def _all_cached_intents(self) -> tuple[object, ...]:
        flattened: list[object] = []
        for input_id in self.dependencies.input_ids:
            flattened.extend(self._intent_cache.get(input_id, ()))
        return tuple(flattened)

    def _record_freshness_deadline(
        self,
        input_id: str,
        snapshot: MirrorSnapshot,
    ) -> None:
        deadlines: list[datetime] = []
        for event in snapshot.events:
            raw = event.source_ts or event.observed_ts
            try:
                timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                continue
            source_time = timestamp.astimezone(timezone.utc)
            try:
                deadline = source_time + self.max_quote_age
            except OverflowError:
                # A saturated/very large economic freshness allowance can extend
                # beyond datetime.max. There is then no representable decision time
                # at which this quote expires, so no finite scheduler deadline exists.
                continue
            deadlines.append(deadline)

        deadline = min(deadlines) if deadlines else None
        generation = self._freshness_generations.get(input_id, 0) + 1
        self._freshness_generations[input_id] = generation
        self._freshness_deadlines[input_id] = deadline
        if deadline is not None:
            heapq.heappush(
                self._freshness_heap,
                (deadline, input_id, generation),
            )

    def _set_availability_deadline(
        self,
        input_id: str,
        deadline: datetime | None,
    ) -> None:
        generation = self._availability_generations.get(input_id, 0) + 1
        self._availability_generations[input_id] = generation
        self._availability_deadlines[input_id] = deadline
        if deadline is not None:
            heapq.heappush(
                self._availability_heap,
                (deadline, input_id, generation),
            )

    def _record_availability_deadline(
        self,
        input_id: str,
        as_of: datetime,
    ) -> None:
        """Schedule the next transition when future causal evidence becomes knowable."""

        boundary = as_of.astimezone(timezone.utc)
        deadlines: list[datetime] = []
        causal = self.dependencies.causal_view(input_id)
        for event in causal.events:
            causal_times = MarketMirror._event_causal_times(event)
            if causal_times is None:
                continue
            available_at = max(causal_times)
            if boundary < available_at:
                deadlines.append(available_at)

        self._set_availability_deadline(
            input_id,
            min(deadlines) if deadlines else None,
        )

    def _activate_available_inputs(
        self,
        now: datetime,
    ) -> tuple[str, ...]:
        """Invalidate inputs exactly when future causal evidence becomes available."""

        activated: list[str] = []
        while self._availability_heap:
            deadline, input_id, generation = self._availability_heap[0]
            current_generation = self._availability_generations.get(input_id)
            current_deadline = self._availability_deadlines.get(input_id)
            if (
                current_generation != generation
                or current_deadline != deadline
            ):
                heapq.heappop(self._availability_heap)
                continue
            if now < deadline:
                break
            heapq.heappop(self._availability_heap)
            self._availability_deadlines[input_id] = None
            activated.append(input_id)
        return tuple(activated)

    def _expire_freshness_inputs(
        self,
        now: datetime,
    ) -> tuple[str, ...]:
        expired: list[str] = []
        while self._freshness_heap:
            deadline, input_id, generation = self._freshness_heap[0]
            current_generation = self._freshness_generations.get(input_id)
            current_deadline = self._freshness_deadlines.get(input_id)
            if (
                current_generation != generation
                or current_deadline != deadline
            ):
                heapq.heappop(self._freshness_heap)
                continue
            if now <= deadline:
                break
            heapq.heappop(self._freshness_heap)
            self._freshness_deadlines[input_id] = None
            expired.append(input_id)
        return tuple(expired)

    def _persist_provider_gap(
        self,
        now: datetime,
        exc: Exception,
    ) -> LiveCycleResult:
        decision_ts = now.isoformat()
        affected = self.dependencies.input_ids
        self._capture_input_views(affected, now, incremental=False)
        market_sha = self._market_state_sha256()
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
            intents=(),
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

    def _decision_identity(
        self,
        *,
        plan: PortfolioPlan,
        market_state_sha256: str,
        gate: str,
        decision_context_sha256: str,
    ) -> tuple[str, str]:
        provenance = self.intent_provenance
        context_payload = {
            "schema": "autosport.live_decision_context",
            "schema_version": 2,
            "loop_id": self.loop_id,
            "mode": self.mode.value,
            "gate": gate,
            "market_state_sha256": market_state_sha256,
            "decision_context_sha256": decision_context_sha256,
            "intent_strategy_version_id": provenance.strategy_version_id,
            "intent_model_version_id": provenance.model_version_id,
            "intent_provenance_sha256": provenance.provenance_sha256,
            "plan_sha256": plan.plan_sha256,
        }
        context_hash = _canonical_json_sha256(context_payload)
        return context_hash, f"live-{context_hash}"

    def _persist_plan(
        self,
        *,
        plan: PortfolioPlan,
        intents: tuple[object, ...],
        market_state_sha256: str,
        affected_input_ids: tuple[str, ...],
        gate: str,
        detail: str = "",
        decision_context_sha256_override: str | None = None,
    ) -> LiveCycleResult:
        if decision_context_sha256_override is None:
            decision_context_sha256 = self._decision_context_sha256()
        else:
            decision_context_sha256 = _canonical_sha256(
                "recovery decision_context_sha256",
                decision_context_sha256_override,
            )
        provenance = self.intent_provenance
        context_hash, decision_id = self._decision_identity(
            plan=plan,
            market_state_sha256=market_state_sha256,
            gate=gate,
            decision_context_sha256=decision_context_sha256,
        )
        prepared_execution: PreparedPaperExecution | None = None
        expected_execution_payload = None
        has_positive_execution_stake = any(stake > 0 for stake in plan.stakes)
        if has_positive_execution_stake and self.paper_execution is None:
            raise LiveDecisionProgressError(
                "positive PAPER/SHADOW plan requires canonical #623 execution adoption"
            )
        if self.paper_execution is not None:
            prepared_execution = self.paper_execution.prepare(
                plan=plan,
                intents=intents,
                decision_id=decision_id,
            )
            if prepared_execution is not None:
                expected_execution_payload = {
                    "schema": "autosport.paper_execution_adoption",
                    "schema_version": 1,
                    "plan_id": prepared_execution.execution_plan.plan_id,
                    "plan_fingerprint": prepared_execution.execution_plan.fingerprint,
                    "model_fingerprint": self.paper_execution.config.fingerprint,
                    "run_id": self.paper_execution.expected_run_id(
                        prepared_execution,
                        decision_id,
                    ),
                    "intent_evidence_json": prepared_execution.intent_evidence_json,
                }

        progress_market_append_generation = (
            None
            if self._progress is None
            else self._progress.market_append_generation
        )
        record_payload = {
            "schema": "autosport.persistent_live_decision",
            "schema_version": 2,
            "loop_id": self.loop_id,
            "mode": self.mode.value,
            "gate": gate,
            "market_state_sha256": market_state_sha256,
            "market_append_generation": progress_market_append_generation,
            "decision_context_sha256": decision_context_sha256,
            "intent_strategy_version_id": provenance.strategy_version_id,
            "intent_model_version_id": provenance.model_version_id,
            "intent_provenance_sha256": provenance.provenance_sha256,
            "affected_input_ids": list(affected_input_ids),
            "plan_sha256": plan.plan_sha256,
            "plan": plan.to_dict(),
            MATERIAL_ACTION_ID_PAYLOAD_KEY: decision_id,
        }
        if expected_execution_payload is not None:
            # Keep the established top-level live-decision schema/version so the
            # decision identity remains stable; execution adoption is additive,
            # separately versioned evidence.
            record_payload["paper_execution"] = expected_execution_payload

        record = DecisionRecord(
            replay_run_id=f"live:{self.loop_id}",
            agent=self.AGENT_ID,
            observed_ts=plan.decision_ts,
            action=f"LIVE_{plan.action.value.upper()}",
            payload=record_payload,
            context_hash=context_hash,
            decision_id=decision_id,
            decision_kind=ECONOMIC_DECISION_KIND,
        )

        duplicate = False
        execution_result = None
        with WorkspaceEconomicLock(self.workspace):
            if (
                decision_context_sha256_override is None
                and self._decision_context_sha256() != decision_context_sha256
            ):
                raise LiveDecisionProgressError(
                    "PaperBook/runtime context changed before promotion lock"
                )
            durable_progress = self._load_progress()
            if (
                durable_progress is None
                or durable_progress.phase
                not in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
                or durable_progress.loop_id != self.loop_id
                or durable_progress.decision_ts != plan.decision_ts
                or durable_progress.market_state_sha256 != market_state_sha256
                or durable_progress.decision_context_sha256
                != decision_context_sha256
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
                current_ledger_end = self._ledger_end_offset()
                if (
                    durable_progress.ledger_offset is not None
                    and current_ledger_end < durable_progress.ledger_offset
                ):
                    raise DecisionLedgerIntegrityError(
                        "pending live decision ledger frontier was truncated"
                    )
                last_record = self._verified_latest_ledger_record(
                    replay_run_id=f"live:{self.loop_id}",
                )
                if (
                    durable_progress.ledger_offset is not None
                    and last_record is not None
                    and last_record[0] >= durable_progress.ledger_offset
                ):
                    raise LiveDecisionProgressError(
                        "pending live progress was superseded before ledger publication"
                    )
                if (
                    last_record is not None
                    and last_record[1].decision_id == decision_id
                ):
                    ledger_offset = last_record[0]
                else:
                    ledger_offset = current_ledger_end
                durable_progress = _Progress(
                    loop_id=self.loop_id,
                    phase=_PHASE_APPEND_PENDING,
                    decision_ts=plan.decision_ts,
                    market_state_sha256=market_state_sha256,
                    market_append_generation=durable_progress.market_append_generation,
                    decision_context_sha256=decision_context_sha256,
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
                    market_append_generation=durable_progress.market_append_generation,
                    decision_context_sha256=decision_context_sha256,
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
                    or existing.payload.get("market_append_generation")
                    != durable_progress.market_append_generation
                    or existing.payload.get("decision_context_sha256")
                    != decision_context_sha256
                    or existing.payload.get("intent_strategy_version_id")
                    != provenance.strategy_version_id
                    or existing.payload.get("intent_model_version_id")
                    != provenance.model_version_id
                    or existing.payload.get("intent_provenance_sha256")
                    != provenance.provenance_sha256
                    or existing.payload.get("gate") != gate
                    or existing.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
                    != decision_id
                ):
                    raise DecisionLedgerIntegrityError(
                        "reserved live decision identity conflicts with durable evidence"
                    )
                if existing.payload.get("paper_execution") != expected_execution_payload:
                    raise DecisionLedgerIntegrityError(
                        "durable live decision execution-adoption evidence changed"
                    )
                duplicate = True
            else:
                self.decision_ledger.append_economic(record, self.authority)
                if self.post_append_hook is not None:
                    self.post_append_hook()

            # Execution attempts are durable before progress becomes COMMITTED.
            # A crash after the #623 attempt but before PaperBook materialization
            # therefore re-enters this same append-pending identity and resumes
            # the exact run instead of fabricating a fresh fill.
            if prepared_execution is not None:
                execution_result = self.paper_execution.execute(
                    prepared=prepared_execution,
                    trigger_id=decision_id,
                    started_at=plan.decision_ts,
                    materialize_exposure=(self.mode is LiveDecisionMode.PAPER),
                )
                assert expected_execution_payload is not None
                if execution_result.run.run_id != expected_execution_payload["run_id"]:
                    raise DecisionLedgerIntegrityError(
                        "durable PAPER execution run identity drifted after decision publication"
                    )

            committed = _Progress(
                loop_id=self.loop_id,
                phase=_PHASE_COMMITTED,
                decision_ts=plan.decision_ts,
                market_state_sha256=market_state_sha256,
                market_append_generation=durable_progress.market_append_generation,
                decision_context_sha256=decision_context_sha256,
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
            paper_execution_run_id=(
                None if execution_result is None else execution_result.run.run_id
            ),
            paper_execution_attempt_ids=(
                ()
                if execution_result is None
                else tuple(
                    attempt.attempt_id for attempt in execution_result.run.attempts
                )
            ),
        )

    def _write_pending(
        self,
        *,
        decision_ts: str,
        market_state_sha256: str,
        affected_input_ids: tuple[str, ...],
        gate: str,
    ) -> None:
        _, decision_time = _canonical_timestamp("decision_ts", decision_ts)
        self.intent_provenance.assert_available_at(decision_time)
        market_append_generation = (
            self._decision_market_append_generation
            if self._decision_market_frontier_as_of == decision_time
            else None
        )
        with WorkspaceEconomicLock(self.workspace):
            durable_control = self._load_control()
            if durable_control is None:
                durable_control = _Control(self.loop_id, LiveControlState.RUNNING)
            if (
                durable_control != self._control
                or durable_control.state is not LiveControlState.RUNNING
            ):
                raise LiveDecisionProgressError(
                    "live decision control changed concurrently before pending publication"
                )

            durable_progress = self._load_progress()
            if durable_progress != self._progress:
                raise LiveDecisionProgressError(
                    "live decision progress changed concurrently before pending publication"
                )
            durable_input_specs = self._load_input_registry() or ()
            if durable_input_specs != tuple(self._input_specs.values()):
                raise LiveDecisionProgressError(
                    "live dependency registry changed concurrently before pending publication"
                )

            # The snapshot is written before the cursor: a crash before cursor
            # publication leaves only ignorable stale snapshot bytes, while every
            # visible PENDING cursor has an exact pre-action portfolio witness.
            #
            # The pre-action artifact is recovery evidence, not an alternate
            # persistence path for the live PaperBook.  In PAPER mode #623 owns
            # that exact live object at workspace/paper_book.json; publishing the
            # same object here would either rebind or cross-path-save its durable
            # generation.  Reuse the canonical risk shadow capability to create
            # a detached exact semantic clone with product-issued opening/causal
            # authority but no live generation/path binding.  Its first save
            # therefore establishes only the dedicated recovery-snapshot lineage.
            live_context_sha256 = self._decision_context_sha256()
            snapshot = self.authority.risk_policy._shadow_book_for_allocation(
                self.book
            )
            if (
                type(snapshot) is not PaperBook
                or snapshot is self.book
                or not self._same_book_state(snapshot, self.book)
            ):
                raise LiveDecisionProgressError(
                    "cannot detach exact pre-action PaperBook"
                )
            snapshot_context_sha256 = self._decision_context_sha256_for_book(
                snapshot
            )
            if snapshot_context_sha256 != live_context_sha256:
                raise LiveDecisionProgressError(
                    "pre-action PaperBook context changed before durability"
                )

            # A new detached shadow is created for every decision cycle so the live
            # canonical PaperBook never acquires the recovery-artifact path authority.
            # When a previous pre-action artifact already exists, explicitly adopt
            # that artifact's *current* durable generation before replacement.  This
            # is a product-owned capability resolved from the sealed persistence graph
            # below; generic PaperBook.save() remains fail-closed for unbound/stale
            # objects and for cross-path publication.
            _paperbook_authority._bind_book(snapshot, self.pre_action_book_path)
            snapshot.save(self.pre_action_book_path)
            durable_pre_action = PaperBook.load(self.pre_action_book_path)
            durable_context_sha256 = self._decision_context_sha256_for_book(
                durable_pre_action
            )
            current_context_sha256 = self._decision_context_sha256()
            if (
                not self._same_book_state(durable_pre_action, self.book)
                or durable_context_sha256 != live_context_sha256
                or current_context_sha256 != live_context_sha256
            ):
                raise LiveDecisionProgressError(
                    "pre-action PaperBook durability verification failed"
                )
            pending = _Progress(
                loop_id=self.loop_id,
                phase=_PHASE_PENDING,
                decision_ts=decision_ts,
                market_state_sha256=market_state_sha256,
                market_append_generation=market_append_generation,
                decision_context_sha256=durable_context_sha256,
                affected_input_ids=affected_input_ids,
                registered_input_ids=self.dependencies.input_ids,
                decision_id=None,
                plan_sha256=None,
                ledger_offset=self._ledger_end_offset(),
                gate=gate,
            )
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
        if raw["schema"] != _INPUTS_SCHEMA or raw["schema_version"] not in {1, _INPUTS_VERSION}:
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
            durable_progress = self._load_progress()
            if durable_progress != self._progress:
                raise LiveDecisionProgressError(
                    "live decision progress changed concurrently before dependency publication"
                )
            if (
                durable_progress is not None
                and durable_progress.phase in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
            ):
                raise LiveDecisionProgressError(
                    "cannot mutate live dependency registry while a decision is unfinished"
                )

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

    def _verified_latest_ledger_record(
        self,
        *,
        replay_run_id: str | None = None,
    ) -> tuple[int, DecisionRecord] | None:
        """Read verified ledger records backwards until the requested lineage is found."""

        if replay_run_id is not None:
            _canonical_text("replay_run_id", replay_run_id)
        path = self.decision_ledger.path
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                if size == 0:
                    return None
                handle.seek(size - 1)
                if handle.read(1) != b"\n":
                    raise DecisionLedgerIntegrityError(
                        "Decision Ledger has an unterminated final record"
                    )

                end = size - 1
                while end >= 0:
                    position = end
                    pieces: list[bytes] = []
                    offset = 0
                    while position > 0:
                        start = max(0, position - 8192)
                        handle.seek(start)
                        chunk = handle.read(position - start)
                        marker = chunk.rfind(b"\n")
                        if marker >= 0:
                            offset = start + marker + 1
                            pieces.append(chunk[marker + 1 :])
                            break
                        pieces.append(chunk)
                        position = start
                    else:
                        offset = 0

                    line = b"".join(reversed(pieces)) + b"\n"
                    if line == b"\n":
                        raise DecisionLedgerIntegrityError(
                            "Decision Ledger contains a blank final record"
                        )
                    JsonlDecisionLedger._verify_bytes(line)
                    envelope = json.loads(line.decode("utf-8"))
                    record = DecisionRecord(
                        **JsonlDecisionLedger._validate_record(envelope["record"])
                    )
                    if replay_run_id is None or record.replay_run_id == replay_run_id:
                        return offset, record
                    if offset == 0:
                        return None
                    end = offset - 1
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DecisionLedgerIntegrityError(
                "Decision Ledger final record is unreadable"
            ) from exc
        return None

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
        payload_version = existing.payload.get("schema_version")
        if payload_version not in {1, 2}:
            raise DecisionLedgerIntegrityError(
                "committed live decision has unsupported schema_version"
            )

        context_payload = {
            "schema": "autosport.live_decision_context",
            "schema_version": payload_version,
            "loop_id": self.loop_id,
            "mode": self.mode.value,
            "gate": progress.gate,
            "market_state_sha256": progress.market_state_sha256,
            "decision_context_sha256": progress.decision_context_sha256,
            "plan_sha256": progress.plan_sha256,
        }
        if payload_version == 2:
            _, committed_decision_time = _canonical_timestamp(
                "committed decision_ts",
                progress.decision_ts,
            )
            try:
                self.intent_provenance.assert_available_at(committed_decision_time)
            except LiveDecisionProgressError as exc:
                raise DecisionLedgerIntegrityError(
                    "committed live decision intent provenance was not causally "
                    "available at decision time"
                ) from exc
            provenance = self.intent_provenance
            if (
                existing.payload.get("intent_strategy_version_id")
                != provenance.strategy_version_id
                or existing.payload.get("intent_model_version_id")
                != provenance.model_version_id
                or existing.payload.get("intent_provenance_sha256")
                != provenance.provenance_sha256
            ):
                raise DecisionLedgerIntegrityError(
                    "committed live decision intent provenance conflicts with canonical registry"
                )
            context_payload.update(
                {
                    "intent_strategy_version_id": provenance.strategy_version_id,
                    "intent_model_version_id": provenance.model_version_id,
                    "intent_provenance_sha256": provenance.provenance_sha256,
                }
            )

        expected_context_hash = _canonical_json_sha256(context_payload)
        expected_decision_id = f"live-{expected_context_hash}"
        if progress.decision_id != expected_decision_id:
            raise DecisionLedgerIntegrityError(
                "committed live progress decision identity is inconsistent"
            )

        if (
            existing.decision_id != progress.decision_id
            or existing.replay_run_id != f"live:{self.loop_id}"
            or existing.agent != self.AGENT_ID
            or existing.observed_ts != progress.decision_ts
            or existing.context_hash != expected_context_hash
            or existing.payload.get("schema")
            != "autosport.persistent_live_decision"
            or existing.payload.get("schema_version") != payload_version
            or existing.payload.get("loop_id") != self.loop_id
            or existing.payload.get("mode") != self.mode.value
            or existing.payload.get("gate") != progress.gate
            or existing.payload.get("market_state_sha256")
            != progress.market_state_sha256
            or existing.payload.get("decision_context_sha256")
            != progress.decision_context_sha256
            or existing.payload.get("affected_input_ids")
            != progress.affected_input_ids
            or existing.payload.get("plan_sha256") != progress.plan_sha256
            or existing.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
            != progress.decision_id
        ):
            raise DecisionLedgerIntegrityError(
                "committed live progress conflicts with Decision Ledger record"
            )

        latest_live = self._verified_latest_ledger_record(
            replay_run_id=f"live:{self.loop_id}",
        )
        if latest_live is None or latest_live[1].decision_id != progress.decision_id:
            raise DecisionLedgerIntegrityError(
                "committed live progress is not the latest durable live decision"
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
        payload: list[dict[str, str]] = []
        for input_id in self.dependencies.input_ids:
            try:
                digest = self._input_market_sha256[input_id]
            except KeyError as exc:
                raise LiveDecisionProgressError(
                    "decision-visible market identity cache is incomplete"
                ) from exc
            payload.append({"input_id": input_id, "sha256": digest})
        return _canonical_json_sha256(payload)

    def _market_state_sha256_for_events(self, events) -> str:
        event_tuple = tuple(events)
        input_hashes: dict[str, str] = {}
        for input_id, spec in self._input_specs.items():
            payload = [
                event.to_dict()
                for event in event_tuple
                if (
                    (spec.source_ids is None or event.source_id in spec.source_ids)
                    and (spec.sports is None or event.sport in spec.sports)
                    and (spec.event_ids is None or event.event_id in spec.event_ids)
                    and (spec.market_ids is None or event.market_id in spec.market_ids)
                    and (
                        spec.selection_ids is None
                        or event.selection_id in spec.selection_ids
                    )
                )
            ]
            input_hashes[input_id] = _canonical_json_sha256(payload)
        return _canonical_json_sha256(
            [
                {"input_id": input_id, "sha256": input_hashes[input_id]}
                for input_id in self.dependencies.input_ids
            ]
        )


# _write_pending is the only live-loop boundary allowed to adopt the existing
# pre-action recovery snapshot generation for a freshly detached risk shadow.  Seal
# that positive capability behind the already-frozen PaperBook persistence graph and
# remove the mutable module alias afterwards.
from ._paperbook_current_binding_verifier import (
    seal_current_binding_consumer as _seal_current_binding_consumer,
)

PersistentLiveDecisionLoop._write_pending = _seal_current_binding_consumer(
    PersistentLiveDecisionLoop._write_pending
)
del _seal_current_binding_consumer
del _paperbook_authority
