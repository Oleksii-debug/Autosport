from __future__ import annotations

import hashlib
import heapq
import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from enum import Enum
from pathlib import Path
from threading import RLock
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
from .market_mirror_health import (
    HealthGatedMirrorDecisionIndex,
    HealthGatedMirrorSnapshot,
    ProviderHealthReplayBoundary,
)
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependency,
    FocusedMirrorDependencyChurnError,
    FocusedMirrorDependencyIndex,
)
from .opportunity import Opportunity, OpportunityContractError, QuoteRef
from .paper import PaperBook
from .paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from .paper_execution_reality import (
    PaperAttemptOutcome,
    PaperExecutionIntegrityError,
    PaperLegAttempt,
    RecoveryDecision,
    _derive_run_economics,
    _synthetic_attempt,
)
from .portfolio_plan import (
    OpportunityEvidence,
    PortfolioDependencyGraph,
    PortfolioPlan,
    build_portfolio_plan,
)
from .providers import MarketProvider, ProviderUnavailableError
from .real_execution_ledger import ExecutionAction, ExecutionPlan
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
_PROGRESS_VERSION = 3
_PROGRESS_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "loop_id",
        "phase",
        "decision_ts",
        "market_state_sha256",
        "market_append_generation",
        "health_boundaries",
        "decision_context_sha256",
        "affected_input_ids",
        "registered_input_ids",
        "decision_id",
        "plan_sha256",
        "ledger_offset",
        "gate",
    }
)
_PROGRESS_KEYS_V2 = _PROGRESS_KEYS - {"health_boundaries"}
_PROGRESS_KEYS_V1 = _PROGRESS_KEYS_V2 - {"market_append_generation"}
_PHASE_PENDING = "pending"
_PHASE_APPEND_PENDING = "append_pending"
_PHASE_COMMITTED = "committed"
_GATE_NORMAL = "normal"
_GATE_PROVIDER_GAP = "provider_gap"
_SHA256_HEX = frozenset("0123456789abcdef")
_CONTROL_SCHEMA = "autosport.live_decision_control"
_CONTROL_VERSION = 1
_CONTROL_KEYS = frozenset({"schema", "schema_version", "loop_id", "state"})
_CONTROL_AUTHORITY_DOMAIN = "autosport.live-decision-control.v1"
_INPUTS_SCHEMA = "autosport.live_decision_inputs"
_INPUTS_VERSION = 2
_INPUTS_KEYS = frozenset({"schema", "schema_version", "loop_id", "inputs"})
_INPUTS_AUTHORITY_DOMAIN = "autosport.live-decision-inputs.v1"
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
        health_boundaries_raw = (
            [] if schema_version in {1, 2} else raw["health_boundaries"]
        )
        if type(health_boundaries_raw) is not list:
            raise LiveDecisionProgressError(
                "health_boundaries must be a JSON array"
            )
        try:
            health_boundaries = tuple(
                sorted(
                    (
                        ProviderHealthReplayBoundary.from_dict(value)
                        for value in health_boundaries_raw
                    ),
                    key=lambda value: value.source_id,
                )
            )
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
    health_boundaries: tuple[ProviderHealthReplayBoundary, ...]
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
        if type(self.health_boundaries) is not tuple:
            raise LiveDecisionProgressError("health_boundaries must be a tuple")
        health_source_ids = tuple(
            boundary.source_id for boundary in self.health_boundaries
        )
        if health_source_ids != tuple(sorted(set(health_source_ids))):
            raise LiveDecisionProgressError(
                "health_boundaries must be sorted and unique by source_id"
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
            "health_boundaries": [
                boundary.to_dict() for boundary in self.health_boundaries
            ],
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
            if type(schema_version) is int and schema_version == _PROGRESS_VERSION
            else _PROGRESS_KEYS_V2
            if type(schema_version) is int and schema_version == 2
            else _PROGRESS_KEYS_V1
            if type(schema_version) is int and schema_version == 1
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
                health_boundaries=health_boundaries,
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
        self._workspace_authority = self.workspace
        self.loop_id = _canonical_text("loop_id", loop_id)
        self._loop_id_authority = self.loop_id
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
        self._configured_mode = mode
        self.book = book
        self._book_authority = book
        self.authority = authority
        self._economic_authority = authority
        self._economic_contract_authority = authority.contract
        self._economic_contract_sha256_authority = provenance_for(
            authority.contract
        ).contract_sha256
        self._risk_policy_authority = authority.risk_policy
        self._risk_policy_sha256_authority = authority.risk_policy.provenance_sha256
        self.intent_factory = intent_factory
        self._intent_factory_authority = intent_factory
        self._intent_factory_strategy_version_id_authority = factory_strategy_version_id
        self.intent_provenance = intent_provenance
        self._intent_provenance_authority = intent_provenance
        self._intent_provenance_sha256_authority = intent_provenance.provenance_sha256
        self.provider = provider
        self._provider_authority = provider if observation_runner is None else None
        self._provider_source_id_authority = (
            None
            if self._provider_authority is None
            else _canonical_text("provider source_id", self._provider_authority.source_id)
        )
        self.decision_ledger = decision_ledger or JsonlDecisionLedger(
            self._workspace_authority / "decisions.jsonl"
        )
        if self.decision_ledger.path != self._workspace_authority / "decisions.jsonl":
            raise ValueError(
                "decision_ledger must use the canonical live workspace Decision Ledger"
            )
        self._decision_ledger_authority = self.decision_ledger
        if paper_execution is not None:
            if not isinstance(paper_execution, PaperExecutionAdoptionRuntime):
                raise TypeError(
                    "paper_execution must be PaperExecutionAdoptionRuntime or None"
                )
            if paper_execution.book is not book:
                raise ValueError(
                    "paper_execution must materialize into the live loop PaperBook"
                )
            canonical_book_path = self._workspace_authority / "paper_book.json"
            if paper_execution.paper_book_path != canonical_book_path:
                raise ValueError(
                    "paper_execution must persist the canonical live workspace PaperBook"
                )
            canonical_execution_ledger_path = (
                self._workspace_authority / "paper-execution.jsonl"
            )
            if paper_execution.ledger.path != canonical_execution_ledger_path:
                raise ValueError(
                    "paper_execution must use the canonical live workspace execution ledger"
                )
        self.paper_execution = paper_execution
        self._paper_execution_authority = paper_execution
        self._paper_execution_ledger_authority = (
            None if paper_execution is None else paper_execution.ledger
        )
        self._paper_execution_config_authority = (
            None if paper_execution is None else paper_execution.config
        )
        self._paper_execution_model_fingerprint_authority = (
            None if paper_execution is None else paper_execution.config.fingerprint
        )
        self._paper_execution_max_quote_age_authority = (
            None if paper_execution is None else paper_execution.max_quote_age
        )
        self.ingestion_policy = ingestion_policy
        self._ingestion_policy_authority = ingestion_policy
        self._ingestion_policy_semantics_authority = (
            None
            if ingestion_policy is None
            else (
                ingestion_policy.max_batch_size,
                ingestion_policy.stale_after_seconds,
                ingestion_policy.max_future_skew_seconds,
            )
        )
        self.max_quote_age = max_quote_age
        self._max_quote_age_authority = max_quote_age
        self.bounds = bounds or LiveLoopBounds()
        self._bounds_authority = self.bounds
        self._bounds_semantics_authority = (
            self.bounds.observation_max_items,
            self.bounds.max_dirty_keys,
            self.bounds.max_dirty_per_cycle,
            self.bounds.max_registered_inputs,
        )
        self.clock = resolved_clock
        self._clock_authority = self.clock
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
        self._catalog_lifecycle_authority = catalog_lifecycle
        self._catalog_fetch_page_authority = catalog_fetch_page
        self._catalog_source_id_authority = catalog_source_id
        self._catalog_required_history_authority = catalog_required_history
        self._default_market_store: SQLiteMarketStore | None = None
        self._default_health_store: SourceHealthStore | None = None
        self._default_market_change_token: int | None = None

        self.progress_path = self._workspace_authority / self.PROGRESS_FILE_NAME
        self.pre_action_book_path = (
            self._workspace_authority / self.PRE_ACTION_BOOK_FILE_NAME
        )
        self.control_path = self._workspace_authority / self.CONTROL_FILE_NAME
        self._progress_path_authority = self.progress_path
        self._pre_action_book_path_authority = self.pre_action_book_path
        self._control_path_authority = self.control_path
        self._control_authority = MonotonicWorkspaceAuthority(
            workspace=self._workspace_authority.resolve(strict=False),
            domain=_CONTROL_AUTHORITY_DOMAIN,
            key=self.loop_id,
        )
        self._control_authority_object = self._control_authority
        self._control_authority_namespace = self._control_authority.namespace_sha256
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

        unfinished_generation = (
            self._progress.market_append_generation
            if self._progress is not None
            and self._progress.phase in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
            else None
        )
        if unfinished_generation is None:
            store = SQLiteMarketStore(self.workspace / "market.db")
            try:
                mirror = MarketMirror.from_store(store)
            finally:
                store.close()
        else:
            store = SQLiteMarketStore.open_frozen_prefix_reader(
                self.workspace / "market.db"
            )
            try:
                mirror = MarketMirror._from_proven_history(
                    store.events_at_committed_append_boundary(
                        unfinished_generation
                    )
                )
            finally:
                store.close()
        self.mirror_updates = BoundedMirrorInvalidationBuffer(
            mirror,
            max_dirty_keys=self.bounds.max_dirty_keys,
        )
        self.dependencies = FocusedMirrorDependencyIndex(mirror)
        self._dependency_mutation_lock = RLock()
        self.inputs_path = self._workspace_authority / self.INPUTS_FILE_NAME
        self._inputs_path_authority = self.inputs_path
        self._inputs_authority = MonotonicWorkspaceAuthority(
            workspace=self._workspace_authority.resolve(strict=False),
            domain=_INPUTS_AUTHORITY_DOMAIN,
            key=self.loop_id,
        )
        self._inputs_authority_object = self._inputs_authority
        self._inputs_authority_namespace = self._inputs_authority.namespace_sha256
        with WorkspaceEconomicLock(self.workspace):
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

        self._pending_dependency_revisions: tuple[tuple[str, int], ...] | None = None
        if (
            self._progress is not None
            and self._progress.phase in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
        ):
            self._pending_dependency_revisions = tuple(
                (dependency.input_id, revision)
                for dependency, revision in self.dependencies.registry_state_snapshot()
            )

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
        self._observe_authority = self._observe

        if self.decision_ledger.path.exists():
            def verify_decision_history(
                committed_market_history: tuple[
                    tuple[MarketEvent, int], ...
                ]
                | None = None,
            ) -> None:
                with WorkspaceEconomicLock(self.workspace):
                    self.decision_ledger.verify_integrity()
                    if self._progress is None:
                        live_run_id = f"live:{self.loop_id}"
                        if self._verified_latest_ledger_record(
                            replay_run_id=live_run_id,
                        ) is not None:
                            raise LiveDecisionProgressError(
                                "durable live decision history exists but "
                                "progress is missing"
                            )
                    if (
                        self._progress is not None
                        and self._progress.phase == _PHASE_COMMITTED
                    ):
                        self._verify_committed_progress_ledger_binding(
                            self._progress,
                            committed_market_history=committed_market_history,
                        )

            committed_generation = (
                self._progress.market_append_generation
                if self._progress is not None
                and self._progress.phase == _PHASE_COMMITTED
                else None
            )
            if committed_generation is None:
                verify_decision_history()
            else:
                prefix_reader = (
                    SQLiteMarketStore.open_frozen_prefix_reader(
                        self.workspace / "market.db"
                    )
                )
                try:
                    with prefix_reader._guard_committed_append_boundary(
                        committed_generation
                    ) as committed_history:
                        verify_decision_history(
                            tuple(committed_history)
                        )
                finally:
                    prefix_reader.close()
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
        with WorkspaceEconomicLock(self.workspace):
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
        owns_store = store is None
        if store is None:
            # Custom observation runners still participate in the canonical durable
            # market authority. Open one bounded verifier connection so their crash
            # cursor gets the same append-generation frontier as the default provider
            # path instead of falling back to timestamp-only recovery.
            store = SQLiteMarketStore(self.workspace / "market.db")

        try:
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
                    force=owns_store,
                )
                # Sample only a cheap, explicitly untrusted generation hint inside the
                # same token interval as the decision clock. A material cycle proves this
                # exact boundary immediately before PENDING publication; idle cycles never
                # pay the full append-history authority proof.
                append_generation = store.append_generation_hint()
                decision_time = self._sample_clock()
                frozen_history: tuple[tuple[MarketEvent, int], ...] | None = None
                dependency_ids = self.dependencies.input_ids
                try:
                    dependency_revisions = tuple(
                        (
                            input_id,
                            self.dependencies.dependency_revision(input_id),
                        )
                        for input_id in dependency_ids
                    )
                    history_required = (
                        self._decision_refresh_may_need_history(decision_time)
                        and any(
                            self.dependencies.requires_current_history_fallback(
                                input_id,
                                as_of=decision_time,
                            )
                            for input_id in dependency_ids
                        )
                    )
                    registry_stable = (
                        self.dependencies.input_ids == dependency_ids
                        and all(
                            self.dependencies.dependency_revision(input_id)
                            == revision
                            for input_id, revision in dependency_revisions
                        )
                    )
                except KeyError:
                    registry_stable = False
                    history_required = False
                if not registry_stable:
                    # A focused selector was registered, retired, or replaced while
                    # history need was being classified. Re-sample the complete
                    # market frontier rather than freezing history for a stale
                    # dependency incarnation.
                    continue
                if history_required:
                    frozen_history = tuple(store.events_with_append_generation())

                if store.external_change_token() == expected_token:
                    self._decision_market_frontier_as_of = decision_time
                    self._decision_market_append_generation = append_generation
                    self._decision_market_history = frozen_history
                    self._decision_market_history_frozen = True
                    return decision_time
        finally:
            if owns_store:
                store.close()

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

    def _restore_dependency_registry(
        self,
        specs: tuple[_InputSpec, ...],
    ) -> None:
        """Restore exact pre-mutation focused/durable-process registry ordering."""
        with self.dependencies.registry_mutation_guard():
            for current_id in self.dependencies.input_ids:
                if not self.dependencies.unregister(current_id):
                    raise LiveDecisionProgressError(
                        "live dependency rollback could not clear current registry"
                    )
            self._input_specs.clear()
            for spec in specs:
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
        # Rebuilding the focused registry creates fresh incarnation tokens. Any
        # process-local intent/freshness cache was derived from the pre-failure
        # incarnations and must be re-established before another economic decision.
        self._needs_cache_rebuild = True

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
        with self._dependency_mutation_lock:
            self._register_input_locked(
                input_id,
                source_ids=source_ids,
                sports=sports,
                event_ids=event_ids,
                market_ids=market_ids,
                selection_ids=selection_ids,
            )

    def _register_input_locked(
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
            focused = tuple(
                dependency
                for dependency in self.dependencies.registry_snapshot()
                if dependency.input_id == normalized_id
            )
            if (
                len(focused) != 1
                or _InputSpec.from_dependency(focused[0]) != existing
            ):
                raise LiveDecisionProgressError(
                    "live dependency registry is inconsistent during idempotent registration"
                )
            return
        if (
            self._progress is not None
            and self._progress.phase in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
        ):
            raise LiveDecisionProgressError(
                "cannot mutate live dependency registry while a decision is unfinished"
            )
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
            self._restore_dependency_registry(previous_specs)
            raise
        self._pending_affected[dependency.input_id] = None
        self._needs_cache_rebuild = True

    def unregister_input(self, input_id: str) -> bool:
        with self._dependency_mutation_lock:
            return self._unregister_input_locked(input_id)

    def _unregister_input_locked(self, input_id: str) -> bool:
        normalized_id = FocusedMirrorDependencyIndex._input_id(input_id)
        existing = self._input_specs.get(normalized_id)
        if existing is None:
            if any(
                dependency.input_id == normalized_id
                for dependency in self.dependencies.registry_snapshot()
            ):
                raise LiveDecisionProgressError(
                    "live dependency registry contains an undurable ghost registration"
                )
            return False
        if (
            self._progress is not None
            and self._progress.phase in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
        ):
            raise LiveDecisionProgressError(
                "cannot mutate live dependency registry while a decision is unfinished"
            )
        previous_specs = tuple(self._input_specs.values())
        if not self.dependencies.unregister(normalized_id):
            raise LiveDecisionProgressError(
                "live dependency registry is inconsistent during retirement"
            )
        self._input_specs.pop(normalized_id)
        try:
            self._persist_input_registry(expected_previous=previous_specs)
        except BaseException:
            # Registry order participates in durable progress identity. Any failed
            # publication must leave the complete process-local registry exactly at
            # its pre-mutation semantic state, not append a retired input at the end.
            self._restore_dependency_registry(previous_specs)
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

        self._assert_canonical_persistence_authority()
        with WorkspaceEconomicLock(self._workspace_authority):
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

        registered_dependency_state = self.dependencies.registry_state_snapshot()
        registered_dependencies = tuple(
            dependency for dependency, _revision in registered_dependency_state
        )
        expected_dependency_revisions = tuple(
            (dependency.input_id, revision)
            for dependency, revision in registered_dependency_state
        )
        registered_input_ids = tuple(
            dependency.input_id for dependency in registered_dependencies
        )
        expected_input_specs = tuple(self._input_specs.values())
        if (
            tuple(
                _InputSpec.from_dependency(dependency)
                for dependency in registered_dependencies
            )
            != expected_input_specs
        ):
            raise LiveDecisionProgressError(
                "live dependency registry diverged before snapshot capture"
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
        snapshots = self._capture_input_views(refresh_input_ids, decision_time)
        current_market_sha = self._market_state_sha256()

        clean_committed_restart = (
            self._needs_cache_rebuild
            and self._progress is not None
            and self._progress.phase == _PHASE_COMMITTED
            and self._progress.gate == _GATE_NORMAL
            and self._progress.market_state_sha256 == current_market_sha
            and self._progress.registered_input_ids == registered_input_ids
            and tuple(self._input_specs.values()) == expected_input_specs
            and tuple(
                _InputSpec.from_dependency(dependency)
                for dependency in self.dependencies.registry_snapshot()
            )
            == expected_input_specs
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
            expected_input_specs=expected_input_specs,
            expected_dependency_revisions=expected_dependency_revisions,
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
        context_payload = {
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
        if self.paper_execution is not None:
            context_payload["paper_execution_model_fingerprint"] = (
                self.paper_execution.config.fingerprint
            )
        return _canonical_json_sha256(context_payload)

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
        with WorkspaceEconomicLock(self.workspace):
            durable_input_specs = self._load_input_registry() or ()
        current_input_specs = tuple(self._input_specs.values())
        focused_dependency_state = self.dependencies.registry_state_snapshot()
        focused_input_specs = tuple(
            _InputSpec.from_dependency(dependency)
            for dependency, _revision in focused_dependency_state
        )
        focused_dependency_revisions = tuple(
            (dependency.input_id, revision)
            for dependency, revision in focused_dependency_state
        )
        durable_input_ids = tuple(spec.input_id for spec in durable_input_specs)
        if (
            progress.registered_input_ids != durable_input_ids
            or current_input_specs != durable_input_specs
            or focused_input_specs != durable_input_specs
            or self._pending_dependency_revisions is None
            or focused_dependency_revisions != self._pending_dependency_revisions
        ):
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

        self._refresh_intents_from_replay(
            progress.registered_input_ids,
            decision_time,
            expected_market_state_sha256=progress.market_state_sha256,
            max_append_generation=progress.market_append_generation,
            refresh_intents=progress.gate == _GATE_NORMAL,
        )
        intents = (
            self._all_cached_intents()
            if progress.gate == _GATE_NORMAL
            else ()
        )

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
        refresh_intents: bool = True,
    ) -> None:
        _canonical_sha256(
            "expected replay market_state_sha256",
            expected_market_state_sha256,
        )
        if type(refresh_intents) is not bool:
            raise TypeError("refresh_intents must be a bool")
        store = (
            SQLiteMarketStore(self.workspace / "market.db")
            if max_append_generation is None
            else SQLiteMarketStore.open_frozen_prefix_reader(
                self.workspace / "market.db"
            )
        )
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
        if not refresh_intents:
            return

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
            intents = self._validated_intents(produced)
            self._require_intents_bound_to_snapshot(intents, focused)
            self._intent_cache[input_id] = intents

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

    @staticmethod
    def _require_intents_bound_to_snapshot(
        intents: tuple[object, ...],
        snapshot: MirrorSnapshot,
    ) -> None:
        """Reject canonical intents whose quote bytes did not come from this view."""

        if not isinstance(snapshot, MirrorSnapshot):
            raise TypeError("snapshot must be MirrorSnapshot")
        for intent in intents:
            for quote in intent.opportunity.quotes:
                matches = tuple(
                    event
                    for event in snapshot.events
                    if QuoteRef.from_market_event(
                        event,
                        market_snapshot_hash=quote.market_snapshot_hash,
                    )
                    == quote
                )
                if len(matches) != 1:
                    raise LiveDecisionProgressError(
                        "live intent quote is not bound to focused market snapshot"
                    )

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
                for _attempt in range(_MARKET_FRONTIER_RETRY_LIMIT):
                    dependency_revision = self.dependencies.dependency_revision(
                        input_id
                    )
                    history_fallback = (
                        self.dependencies.requires_current_history_fallback(
                            input_id,
                            as_of=as_of,
                        )
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
                        next_history_availability = (
                            self._next_availability_deadline(input_id, as_of)
                        )

                    if (
                        self.dependencies.dependency_revision(input_id)
                        == dependency_revision
                    ):
                        break
                    # The focused selector changed between history classification
                    # and snapshot/deadline materialization. Retry the complete
                    # decision read so a replacement that requires durable-history
                    # fallback cannot be evaluated through latest-only mirror truth.
                else:
                    raise LiveDecisionProgressError(
                        "focused dependency changed continuously during snapshot capture"
                    )

                snapshots[input_id] = snapshot
                self._input_market_sha256[input_id] = _canonical_json_sha256(
                    [event.to_dict() for event in snapshot.events]
                )
                self._record_freshness_deadline(input_id, snapshot)
                self._set_availability_deadline(
                    input_id,
                    next_history_availability,
                )
        except FocusedMirrorDependencyChurnError as exc:
            raise LiveDecisionProgressError(
                "focused dependency changed continuously during snapshot capture"
            ) from exc
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
            intents = self._validated_intents(produced)
            self._require_intents_bound_to_snapshot(intents, snapshot)
            self._intent_cache[input_id] = intents

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

    def _next_availability_deadline(
        self,
        input_id: str,
        as_of: datetime,
    ) -> datetime | None:
        """Return when future causal evidence next becomes knowable."""

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

        return min(deadlines) if deadlines else None

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
        registered_dependency_state = self.dependencies.registry_state_snapshot()
        registered_dependencies = tuple(
            dependency for dependency, _revision in registered_dependency_state
        )
        expected_dependency_revisions = tuple(
            (dependency.input_id, revision)
            for dependency, revision in registered_dependency_state
        )
        affected = tuple(
            dependency.input_id for dependency in registered_dependencies
        )
        expected_input_specs = tuple(self._input_specs.values())
        if (
            tuple(
                _InputSpec.from_dependency(dependency)
                for dependency in registered_dependencies
            )
            != expected_input_specs
        ):
            raise LiveDecisionProgressError(
                "live dependency registry diverged before provider-gap snapshot capture"
            )
        self._capture_input_views(affected, now, incremental=False)
        market_sha = self._market_state_sha256()
        self._write_pending(
            decision_ts=decision_ts,
            market_state_sha256=market_sha,
            affected_input_ids=affected,
            gate=_GATE_PROVIDER_GAP,
            expected_input_specs=expected_input_specs,
            expected_dependency_revisions=expected_dependency_revisions,
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

    def _assert_canonical_persistence_authority(self) -> None:
        if self.workspace != self._workspace_authority:
            raise LiveDecisionProgressError(
                "live workspace persistence authority changed after construction"
            )
        if self.loop_id != self._loop_id_authority:
            raise LiveDecisionProgressError(
                "live loop identity authority changed after construction"
            )
        if self.mode is not self._configured_mode:
            raise LiveDecisionProgressError(
                "live decision mode authority changed after decision preparation"
            )
        if self.progress_path != self._progress_path_authority:
            raise LiveDecisionProgressError(
                "live progress path authority changed after construction"
            )
        if self.pre_action_book_path != self._pre_action_book_path_authority:
            raise LiveDecisionProgressError(
                "pre-action PaperBook path authority changed after construction"
            )
        if self.control_path != self._control_path_authority:
            raise LiveDecisionProgressError(
                "live control path authority changed after construction"
            )
        if (
            self._control_authority is not self._control_authority_object
            or self._control_authority.namespace_sha256
            != self._control_authority_namespace
            or self._control_authority.domain != _CONTROL_AUTHORITY_DOMAIN
            or self._control_authority.key != self._loop_id_authority
        ):
            raise LiveDecisionProgressError(
                "live control monotonic authority changed after construction"
            )
        if self.inputs_path != self._inputs_path_authority:
            raise LiveDecisionProgressError(
                "live input-registry path authority changed after construction"
            )
        if (
            self._inputs_authority is not self._inputs_authority_object
            or self._inputs_authority.namespace_sha256
            != self._inputs_authority_namespace
            or self._inputs_authority.domain != _INPUTS_AUTHORITY_DOMAIN
            or self._inputs_authority.key != self._loop_id_authority
        ):
            raise LiveDecisionProgressError(
                "live input-registry monotonic authority changed after construction"
            )
        if self.book is not self._book_authority:
            raise LiveDecisionProgressError(
                "live PaperBook authority changed after construction"
            )
        if self.authority is not self._economic_authority:
            raise LiveDecisionProgressError(
                "economic decision authority changed after construction"
            )
        if self.authority.contract is not self._economic_contract_authority:
            raise LiveDecisionProgressError(
                "economic goal contract authority changed after construction"
            )
        if self.authority.risk_policy is not self._risk_policy_authority:
            raise LiveDecisionProgressError(
                "risk policy authority changed after construction"
            )
        if (
            provenance_for(self.authority.contract).contract_sha256
            != self._economic_contract_sha256_authority
        ):
            raise LiveDecisionProgressError(
                "economic goal contract semantics changed after construction"
            )
        if (
            self.authority.risk_policy.provenance_sha256
            != self._risk_policy_sha256_authority
        ):
            raise LiveDecisionProgressError(
                "risk policy semantics changed after construction"
            )
        if self.intent_factory is not self._intent_factory_authority:
            raise LiveDecisionProgressError(
                "live intent factory authority changed after construction"
            )
        if (
            getattr(self.intent_factory, "strategy_version_id", None)
            != self._intent_factory_strategy_version_id_authority
        ):
            raise LiveDecisionProgressError(
                "live intent factory strategy identity changed after construction"
            )
        if self.intent_provenance is not self._intent_provenance_authority:
            raise LiveDecisionProgressError(
                "live intent provenance authority changed after construction"
            )
        if (
            self.intent_provenance.provenance_sha256
            != self._intent_provenance_sha256_authority
        ):
            raise LiveDecisionProgressError(
                "live intent provenance semantics changed after construction"
            )
        if self.ingestion_policy is not self._ingestion_policy_authority:
            raise LiveDecisionProgressError(
                "live ingestion policy authority changed after construction"
            )
        ingestion_policy_semantics = (
            None
            if self.ingestion_policy is None
            else (
                self.ingestion_policy.max_batch_size,
                self.ingestion_policy.stale_after_seconds,
                self.ingestion_policy.max_future_skew_seconds,
            )
        )
        if ingestion_policy_semantics != self._ingestion_policy_semantics_authority:
            raise LiveDecisionProgressError(
                "live ingestion policy semantics changed after construction"
            )
        if self._observe is not self._observe_authority:
            raise LiveDecisionProgressError(
                "live observation authority changed after construction"
            )
        if self._default_health_store is not None:
            try:
                self._default_health_store.assert_persistence_authority()
            except RuntimeError as exc:
                raise LiveDecisionProgressError(
                    "live source-health persistence authority changed after construction"
                ) from exc
        if self._provider_authority is not None:
            if self.provider is not self._provider_authority:
                raise LiveDecisionProgressError(
                    "live market provider authority changed after construction"
                )
            if (
                getattr(self.provider, "source_id", None)
                != self._provider_source_id_authority
            ):
                raise LiveDecisionProgressError(
                    "live market provider source identity changed after construction"
                )
        if self.max_quote_age != self._max_quote_age_authority:
            raise LiveDecisionProgressError(
                "live quote-age authority changed after construction"
            )
        if self.bounds is not self._bounds_authority:
            raise LiveDecisionProgressError(
                "live loop bounds authority changed after construction"
            )
        bounds_semantics = (
            self.bounds.observation_max_items,
            self.bounds.max_dirty_keys,
            self.bounds.max_dirty_per_cycle,
            self.bounds.max_registered_inputs,
        )
        if bounds_semantics != self._bounds_semantics_authority:
            raise LiveDecisionProgressError(
                "live loop bounds semantics changed after construction"
            )
        if self.clock is not self._clock_authority:
            raise LiveDecisionProgressError(
                "live clock authority changed after construction"
            )
        if self.catalog_lifecycle is not self._catalog_lifecycle_authority:
            raise LiveDecisionProgressError(
                "catalog lifecycle authority changed after construction"
            )
        if self.catalog_fetch_page is not self._catalog_fetch_page_authority:
            raise LiveDecisionProgressError(
                "catalog fetch authority changed after construction"
            )
        if self.catalog_source_id != self._catalog_source_id_authority:
            raise LiveDecisionProgressError(
                "catalog source authority changed after construction"
            )
        if self.catalog_required_history != self._catalog_required_history_authority:
            raise LiveDecisionProgressError(
                "catalog history authority changed after construction"
            )
        if self.decision_ledger is not self._decision_ledger_authority:
            raise LiveDecisionProgressError(
                "live Decision Ledger persistence authority changed after construction"
            )
        try:
            self.decision_ledger.assert_transaction_authority()
        except DecisionLedgerIntegrityError as exc:
            raise LiveDecisionProgressError(
                "live Decision Ledger transaction authority is unavailable"
            ) from exc
        canonical_decision_ledger = self._workspace_authority / "decisions.jsonl"
        if self.decision_ledger.path != canonical_decision_ledger:
            raise LiveDecisionProgressError(
                "live Decision Ledger persistence authority changed after construction"
            )
        if self.paper_execution is not self._paper_execution_authority:
            raise LiveDecisionProgressError(
                "PAPER execution runtime authority changed after construction"
            )
        if self.paper_execution is None:
            return
        try:
            self.paper_execution._assert_runtime_authority()
        except PaperExecutionAdoptionError as exc:
            raise LiveDecisionProgressError(
                "PAPER execution runtime authority changed after construction"
            ) from exc
        if self.paper_execution.book is not self._book_authority:
            raise LiveDecisionProgressError(
                "PAPER execution PaperBook authority changed after construction"
            )
        if self.paper_execution.paper_book_path != self._workspace_authority / "paper_book.json":
            raise LiveDecisionProgressError(
                "PAPER execution PaperBook path authority changed after construction"
            )
        if self.paper_execution.ledger is not self._paper_execution_ledger_authority:
            raise LiveDecisionProgressError(
                "PAPER execution ledger authority changed after construction"
            )
        if self.paper_execution.ledger.path != self._workspace_authority / "paper-execution.jsonl":
            raise LiveDecisionProgressError(
                "PAPER execution ledger authority changed after construction"
            )
        if self.paper_execution.config is not self._paper_execution_config_authority:
            raise LiveDecisionProgressError(
                "PAPER execution model authority changed after construction"
            )
        if (
            self.paper_execution.config.fingerprint
            != self._paper_execution_model_fingerprint_authority
        ):
            raise LiveDecisionProgressError(
                "PAPER execution model authority changed after construction"
            )
        if (
            self.paper_execution.max_quote_age
            != self._paper_execution_max_quote_age_authority
        ):
            raise LiveDecisionProgressError(
                "PAPER execution quote-age authority changed after construction"
            )

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
        self._assert_canonical_persistence_authority()
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

        self._assert_canonical_persistence_authority()

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
        execution_guard = (
            nullcontext()
            if self.paper_execution is None
            else self.paper_execution.execution_guard()
        )
        with (
            WorkspaceEconomicLock(self.workspace),
            execution_guard,
            self.dependencies.registry_mutation_guard(),
        ):
            self._assert_canonical_persistence_authority()
            focused_dependency_state = self.dependencies.registry_state_snapshot()
            durable_progress = self._load_progress()
            if (
                durable_progress is not None
                and durable_progress.phase == _PHASE_PENDING
                and self._decision_context_sha256() != decision_context_sha256
            ):
                raise LiveDecisionProgressError(
                    "PaperBook/runtime context changed before promotion lock"
                )
            if (
                durable_progress is not None
                and durable_progress.phase == _PHASE_APPEND_PENDING
                and decision_context_sha256_override is None
                and self._decision_context_sha256() != decision_context_sha256
            ):
                raise LiveDecisionProgressError(
                    "PaperBook/runtime context changed before promotion lock"
                )
            durable_input_specs = self._load_input_registry() or ()
            current_input_specs = tuple(self._input_specs.values())
            focused_input_specs = tuple(
                _InputSpec.from_dependency(dependency)
                for dependency, _revision in focused_dependency_state
            )
            focused_dependency_revisions = tuple(
                (dependency.input_id, revision)
                for dependency, revision in focused_dependency_state
            )
            durable_input_ids = tuple(spec.input_id for spec in durable_input_specs)
            if (
                durable_progress is None
                or durable_progress.phase
                not in {_PHASE_PENDING, _PHASE_APPEND_PENDING}
                or durable_progress.loop_id != self.loop_id
                or durable_progress.decision_ts != plan.decision_ts
                or durable_progress.market_state_sha256 != market_state_sha256
                or durable_progress.market_append_generation
                != progress_market_append_generation
                or durable_progress.decision_context_sha256
                != decision_context_sha256
                or durable_progress.affected_input_ids != affected_input_ids
                or durable_progress.registered_input_ids != durable_input_ids
                or current_input_specs != durable_input_specs
                or focused_input_specs != durable_input_specs
                or self._pending_dependency_revisions is None
                or focused_dependency_revisions != self._pending_dependency_revisions
                or durable_progress.gate != gate
            ):
                raise LiveDecisionProgressError(
                    "live decision progress changed before durable ledger publication"
                )

            if (
                durable_progress.phase == _PHASE_PENDING
                and prepared_execution is not None
            ):
                assert expected_execution_payload is not None
                orphan_execution_events = self.paper_execution.ledger.events(
                    expected_execution_payload["run_id"]
                )
                if orphan_execution_events:
                    raise LiveDecisionProgressError(
                        "pending live decision has orphan #623 execution history"
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
                    registered_input_ids=durable_input_ids,
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
                    registered_input_ids=durable_input_ids,
                    decision_id=decision_id,
                    plan_sha256=plan.plan_sha256,
                    ledger_offset=ledger_offset,
                    gate=gate,
                )
                atomic_write_json(self.progress_path, durable_progress.to_dict())
                self._progress = durable_progress
                existing = self._verified_ledger_record_at_offset(ledger_offset)

            if (
                existing is None
                and prepared_execution is not None
            ):
                assert expected_execution_payload is not None
                orphan_execution_events = self.paper_execution.ledger.events(
                    expected_execution_payload["run_id"]
                )
                if orphan_execution_events:
                    raise LiveDecisionProgressError(
                        "append-pending live decision without durable Decision Ledger "
                        "has orphan #623 execution history"
                    )

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
                self._assert_canonical_persistence_authority()
                if self.dependencies.registry_state_snapshot() != focused_dependency_state:
                    raise LiveDecisionProgressError(
                        "focused dependency registry changed during durable ledger publication"
                    )

            # Execution attempts are durable before progress becomes COMMITTED.
            # A crash after the #623 attempt but before PaperBook materialization
            # therefore re-enters this same append-pending identity and resumes
            # the exact run instead of fabricating a fresh fill.
            if prepared_execution is not None:
                self._assert_canonical_persistence_authority()
                execution_result = self.paper_execution.execute(
                    prepared=prepared_execution,
                    trigger_id=decision_id,
                    started_at=plan.decision_ts,
                    materialize_exposure=(self._configured_mode is LiveDecisionMode.PAPER),
                )
                assert expected_execution_payload is not None
                if execution_result.run.run_id != expected_execution_payload["run_id"]:
                    raise DecisionLedgerIntegrityError(
                        "durable PAPER execution run identity drifted after decision publication"
                    )

            # Execution is an external side-effect boundary. A runtime/configuration
            # mutation that happens while #623 adoption is in flight must not be
            # laundered into a COMMITTED cursor under a different live authority.
            self._assert_canonical_persistence_authority()
            if self.dependencies.registry_state_snapshot() != focused_dependency_state:
                raise LiveDecisionProgressError(
                    "focused dependency registry changed before committed publication"
                )

            committed = _Progress(
                loop_id=self.loop_id,
                phase=_PHASE_COMMITTED,
                decision_ts=plan.decision_ts,
                market_state_sha256=market_state_sha256,
                market_append_generation=durable_progress.market_append_generation,
                decision_context_sha256=decision_context_sha256,
                affected_input_ids=affected_input_ids,
                registered_input_ids=durable_input_ids,
                decision_id=decision_id,
                plan_sha256=plan.plan_sha256,
                ledger_offset=ledger_offset,
                gate=gate,
            )
            atomic_write_json(self.progress_path, committed.to_dict())
            self._progress = committed
            self._pending_dependency_revisions = None

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
        expected_input_specs: tuple[_InputSpec, ...] | None = None,
        expected_dependency_revisions: tuple[tuple[str, int], ...] | None = None,
    ) -> None:
        self._assert_canonical_persistence_authority()
        _, decision_time = _canonical_timestamp("decision_ts", decision_ts)
        self.intent_provenance.assert_available_at(decision_time)
        market_append_generation = (
            self._decision_market_append_generation
            if self._decision_market_frontier_as_of == decision_time
            else None
        )
        store = self._default_market_store
        owns_store = store is None and market_append_generation is not None
        if owns_store:
            store = SQLiteMarketStore(self.workspace / "market.db")

        def publish_pending() -> tuple[_Progress, tuple[tuple[str, int], ...]]:
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
                current_input_specs = tuple(self._input_specs.values())
                bound_input_specs = (
                    current_input_specs
                    if expected_input_specs is None
                    else expected_input_specs
                )
                if (
                    expected_input_specs is not None
                    and current_input_specs != expected_input_specs
                ):
                    raise LiveDecisionProgressError(
                        "live dependency registry changed after snapshot capture"
                    )
                expected_input_ids = tuple(
                    spec.input_id for spec in bound_input_specs
                )
                current_dependency_state = self.dependencies.registry_state_snapshot()
                current_dependencies = tuple(
                    dependency
                    for dependency, _revision in current_dependency_state
                )
                pending_dependency_revisions = tuple(
                    (dependency.input_id, revision)
                    for dependency, revision in current_dependency_state
                )
                if (
                    tuple(
                        _InputSpec.from_dependency(dependency)
                        for dependency in current_dependencies
                    )
                    != bound_input_specs
                    or (
                        expected_dependency_revisions is not None
                        and pending_dependency_revisions
                        != expected_dependency_revisions
                    )
                ):
                    raise LiveDecisionProgressError(
                        "focused dependency registry changed after snapshot capture"
                    )
                durable_input_specs = self._load_input_registry() or ()
                if durable_input_specs != bound_input_specs:
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
                    registered_input_ids=expected_input_ids,
                    decision_id=None,
                    plan_sha256=None,
                    ledger_offset=self._ledger_end_offset(),
                    gate=gate,
                )
                # Seal the focused dependency incarnation across the durable PENDING
                # publication itself.  The earlier snapshot proves what was captured,
                # while this final guard prevents a direct index replacement/reincarnation
                # from crossing the atomic progress commit after validation.
                with self.dependencies.registry_mutation_guard():
                    focused_state = self.dependencies.registry_state_snapshot()
                    guarded_input_specs = tuple(
                        _InputSpec.from_dependency(dependency)
                        for dependency, _revision in focused_state
                    )
                    guarded_dependency_revisions = tuple(
                        (dependency.input_id, revision)
                        for dependency, revision in focused_state
                    )
                    if (
                        guarded_input_specs != bound_input_specs
                        or guarded_dependency_revisions
                        != pending_dependency_revisions
                    ):
                        raise LiveDecisionProgressError(
                            "focused dependency registry changed concurrently "
                            "before pending publication"
                        )
                    atomic_write_json(self.progress_path, pending.to_dict())
                    self._progress = pending
                    self._pending_dependency_revisions = pending_dependency_revisions
                return pending, pending_dependency_revisions

        try:
            if market_append_generation is None:
                publish_pending()
            else:
                assert store is not None
                # The guard spans both the complete current-tail proof and durable
                # PENDING publication. No cooperating append or direct SQLite writer
                # can change canonical market truth inside this interval.
                with store._guard_current_append_authority_with_boundary(
                    market_append_generation
                ) as durable_history:
                    boundary, age_limit = MarketMirror._decision_boundary(
                        as_of=decision_time,
                        max_age=self.max_quote_age,
                    )
                    durable_snapshot = (
                        MarketMirror._decision_view_from_proven_history(
                            durable_history,
                            boundary=boundary,
                            max_age=age_limit,
                            source_ids=None,
                            sports=None,
                            event_ids=None,
                            market_ids=None,
                            selection_ids=None,
                        )
                    )
                    durable_market_state_sha256 = (
                        self._market_state_sha256_for_events(
                            durable_snapshot.events
                        )
                    )
                    if durable_market_state_sha256 != market_state_sha256:
                        raise LiveDecisionProgressError(
                            "decision-visible market state is not durable at "
                            "sampled append frontier"
                        )
                    publish_pending()
        finally:
            if owns_store:
                assert store is not None
                store.close()

    def _input_registry_payload(
        self,
        specs: tuple[_InputSpec, ...],
    ) -> dict[str, object]:
        return {
            "schema": _INPUTS_SCHEMA,
            "schema_version": _INPUTS_VERSION,
            "loop_id": self.loop_id,
            "inputs": [spec.to_dict() for spec in specs],
        }

    def _input_registry_state_sha256(
        self,
        specs: tuple[_InputSpec, ...] | None,
    ) -> str | None:
        if specs is None:
            return None
        return _canonical_json_sha256(self._input_registry_payload(specs))

    def _input_registry_transition_binding(
        self,
        *,
        previous_state_sha256: str | None,
        candidate: tuple[_InputSpec, ...],
        kind: str,
    ) -> str:
        return _canonical_json_sha256(
            {
                "schema": "autosport.live_decision_inputs_transition",
                "schema_version": 1,
                "kind": kind,
                "loop_id": self.loop_id,
                "previous_state_sha256": previous_state_sha256,
                "intended_state_sha256": self._input_registry_state_sha256(candidate),
                "input_ids": [spec.input_id for spec in candidate],
            }
        )

    @staticmethod
    def _input_registry_tx_id(binding_sha256: str) -> str:
        return f"live-inputs-{binding_sha256}"

    def _read_input_registry_file(self) -> tuple[_InputSpec, ...] | None:
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

    def _load_input_registry(self) -> tuple[_InputSpec, ...] | None:
        specs = self._read_input_registry_file()
        observed = self._input_registry_state_sha256(specs)
        try:
            history = self._inputs_authority.read_history()
            if not history:
                if specs is None:
                    return None
                binding = self._input_registry_transition_binding(
                    previous_state_sha256=None,
                    candidate=specs,
                    kind="BOOTSTRAP",
                )
                tx_id = self._input_registry_tx_id(binding)
                assert observed is not None
                self._inputs_authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                self._inputs_authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                return specs

            pending = history[-1] if history[-1].phase is AuthorityPhase.PREPARE else None
            if pending is not None and observed == pending.intended_state_sha256:
                if specs is None:
                    raise LiveDecisionProgressError(
                        "prepared live input-registry authority has no durable registry bytes"
                    )
                matched: tuple[str, str] | None = None
                for kind in ("TRANSITION", "BOOTSTRAP"):
                    candidate_binding = self._input_registry_transition_binding(
                        previous_state_sha256=pending.previous_committed_state_sha256,
                        candidate=specs,
                        kind=kind,
                    )
                    candidate_tx_id = self._input_registry_tx_id(candidate_binding)
                    if (
                        candidate_tx_id == pending.tx_id
                        and candidate_binding == pending.semantic_binding_sha256
                    ):
                        matched = (candidate_tx_id, candidate_binding)
                        break
                if matched is None:
                    raise LiveDecisionProgressError(
                        "prepared live input-registry authority conflicts with durable registry semantics"
                    )
                tx_id, binding = matched
                self._inputs_authority.recover(
                    observed_state_sha256=observed,
                    tx_id=tx_id,
                    semantic_binding_sha256=binding,
                )
            else:
                self._inputs_authority.recover(
                    observed_state_sha256=observed,
                )
            return specs
        except MonotonicWorkspaceAuthorityError as exc:
            raise LiveDecisionProgressError(
                "live dependency registry failed monotonic rollback/recovery verification"
            ) from exc

    def _persist_input_registry(
        self,
        *,
        expected_previous: tuple[_InputSpec, ...],
    ) -> None:
        self._assert_canonical_persistence_authority()
        candidate = tuple(self._input_specs.values())
        payload = self._input_registry_payload(candidate)
        with WorkspaceEconomicLock(self._workspace_authority):
            self._assert_canonical_persistence_authority()
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
            with self.dependencies.registry_mutation_guard():
                focused_state = self.dependencies.registry_state_snapshot()
                focused_candidate = tuple(
                    _InputSpec.from_dependency(dependency)
                    for dependency, _revision in focused_state
                )
                if focused_candidate != candidate:
                    raise LiveDecisionProgressError(
                        "focused dependency registry changed before dependency publication"
                    )
                observed = self._input_registry_state_sha256(
                    None if not self.inputs_path.exists() else durable
                )
                intended = self._input_registry_state_sha256(candidate)
                assert intended is not None
                binding = self._input_registry_transition_binding(
                    previous_state_sha256=observed,
                    candidate=candidate,
                    kind="TRANSITION",
                )
                tx_id = self._input_registry_tx_id(binding)
                try:
                    self._inputs_authority.prepare(
                        tx_id=tx_id,
                        observed_state_sha256=observed,
                        intended_state_sha256=intended,
                        semantic_binding_sha256=binding,
                    )
                    atomic_write_json(self.inputs_path, payload)
                    published = self._read_input_registry_file()
                    if published != candidate:
                        raise LiveDecisionProgressError(
                            "live dependency registry publication changed before monotonic commit"
                        )
                    self._inputs_authority.commit(
                        tx_id=tx_id,
                        observed_state_sha256=intended,
                        semantic_binding_sha256=binding,
                    )
                except MonotonicWorkspaceAuthorityError as exc:
                    raise LiveDecisionProgressError(
                        "live dependency registry monotonic publication failed"
                    ) from exc

    def _ledger_end_offset(self) -> int:
        snapshot = self.decision_ledger.verified_snapshot_if_exists()
        return len(snapshot.payload)

    def _verified_latest_ledger_record(
        self,
        *,
        replay_run_id: str | None = None,
    ) -> tuple[int, DecisionRecord] | None:
        """Read the latest requested lineage from one verified ledger snapshot."""

        if replay_run_id is not None:
            _canonical_text("replay_run_id", replay_run_id)
        payload = self.decision_ledger.verified_snapshot_if_exists().payload
        if not payload:
            return None

        lines = payload.splitlines(keepends=True)
        offset = len(payload)
        for line in reversed(lines):
            offset -= len(line)
            envelope = json.loads(line.decode("utf-8"))
            record = DecisionRecord(
                **JsonlDecisionLedger._validate_record(envelope["record"])
            )
            if replay_run_id is None or record.replay_run_id == replay_run_id:
                return offset, record
        return None

    def _verified_ledger_record_at_offset(
        self,
        offset: int,
    ) -> DecisionRecord | None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise LiveDecisionProgressError("ledger_offset must be a non-negative integer")
        payload = self.decision_ledger.verified_snapshot_if_exists().payload
        size = len(payload)
        if offset > size:
            raise DecisionLedgerIntegrityError(
                "reserved Decision Ledger offset is beyond durable bytes"
            )
        if offset == size:
            return None
        if offset > 0 and payload[offset - 1 : offset] != b"\n":
            raise DecisionLedgerIntegrityError(
                "reserved Decision Ledger offset is not a record boundary"
            )
        end = payload.find(b"\n", offset)
        if end < 0:
            raise DecisionLedgerIntegrityError(
                "reserved Decision Ledger record is unterminated"
            )
        line = payload[offset : end + 1]
        envelope = json.loads(line.decode("utf-8"))
        record = JsonlDecisionLedger._validate_record(envelope["record"])
        return DecisionRecord(**record)

    def _verify_committed_execution_binding(
        self,
        *,
        existing: DecisionRecord,
        durable_plan: PortfolioPlan,
        progress: _Progress,
        committed_market_history: tuple[
            tuple[MarketEvent, int], ...
        ]
        | None = None,
    ) -> None:
        has_positive_stake = any(stake > 0 for stake in durable_plan.stakes)
        execution_payload = existing.payload.get("paper_execution")
        if not has_positive_stake:
            if execution_payload is not None:
                raise DecisionLedgerIntegrityError(
                    "zero-stake committed live decision carries unexpected "
                    "execution evidence"
                )
            return

        runtime = self.paper_execution
        if runtime is None:
            raise DecisionLedgerIntegrityError(
                "positive committed live decision requires canonical #623 "
                "execution runtime"
            )
        expected_keys = {
            "schema",
            "schema_version",
            "plan_id",
            "plan_fingerprint",
            "model_fingerprint",
            "run_id",
            "intent_evidence_json",
        }
        if (
            type(execution_payload) is not dict
            or set(execution_payload) != expected_keys
            or execution_payload.get("schema")
            != "autosport.paper_execution_adoption"
            or execution_payload.get("schema_version") != 1
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision execution-adoption evidence is invalid"
            )

        try:
            plan_id = _canonical_text(
                "execution-adoption plan_id",
                execution_payload["plan_id"],
            )
            plan_fingerprint = _canonical_text(
                "execution-adoption plan_fingerprint",
                execution_payload["plan_fingerprint"],
            )
            model_fingerprint = _canonical_text(
                "execution-adoption model_fingerprint",
                execution_payload["model_fingerprint"],
            )
            run_id = _canonical_text(
                "execution-adoption run_id",
                execution_payload["run_id"],
            )
            intent_evidence_json = _canonical_text(
                "execution-adoption intent_evidence_json",
                execution_payload["intent_evidence_json"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DecisionLedgerIntegrityError(
                "committed live decision execution-adoption identity is invalid"
            ) from exc

        if model_fingerprint != runtime.config.fingerprint:
            raise DecisionLedgerIntegrityError(
                "committed live decision execution model conflicts with runtime"
            )
        try:
            intent_evidence = strict_json_loads(intent_evidence_json)
            canonical_intent_evidence = json.dumps(
                intent_evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise DecisionLedgerIntegrityError(
                "committed live decision intent execution evidence is invalid"
            ) from exc
        if canonical_intent_evidence != intent_evidence_json:
            raise DecisionLedgerIntegrityError(
                "committed live decision intent execution evidence is not canonical"
            )
        if (
            type(intent_evidence) is not dict
            or set(intent_evidence) != {"schema", "schema_version", "intents"}
            or intent_evidence.get("schema")
            != "autosport.portfolio_plan_intent_evidence"
            or intent_evidence.get("schema_version") != 1
            or type(intent_evidence.get("intents")) is not list
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision intent execution evidence schema is invalid"
            )
        intent_items = intent_evidence["intents"]
        if (
            tuple(
                item.get("intent_id") if type(item) is dict else None
                for item in intent_items
            )
            != durable_plan.intent_ids
            or tuple(
                item.get("intent_sha256") if type(item) is dict else None
                for item in intent_items
            )
            != durable_plan.intent_sha256s
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision intent execution evidence conflicts with plan"
            )
        provenance = self.intent_provenance
        _, evidence_decision_time = _canonical_timestamp(
            "committed decision_ts",
            progress.decision_ts,
        )
        expected_intent_keys = {
            "schema",
            "schema_version",
            "intent_id",
            "intent_sha256",
            "opportunity_id",
            "opportunity",
            "evidence",
            "evidence_sha256",
            "candidate_sha256",
            "signal_strength",
            "strategy_id",
            "model_id",
            "config_sha256",
            "risk_context",
        }
        canonical_opportunities: list[Opportunity] = []
        for item in intent_items:
            if (
                type(item) is not dict
                or set(item) != expected_intent_keys
                or item.get("schema")
                != "autosport.opportunity_intent_evidence"
                or item.get("schema_version") != 1
            ):
                raise DecisionLedgerIntegrityError(
                    "committed live decision intent execution item is invalid"
                )
            if (
                item.get("strategy_id") != provenance.strategy_version_id
                or item.get("model_id") != provenance.model_version_id
                or item.get("config_sha256") != provenance.config_sha256
            ):
                raise DecisionLedgerIntegrityError(
                    "committed live decision intent execution provenance conflicts"
                )
            try:
                opportunity = Opportunity.from_dict(item["opportunity"])
                evidence = OpportunityEvidence.from_dict(item["evidence"])
                signal_strength = Decimal(item["signal_strength"])
                _canonical_sha256(
                    "intent candidate_sha256",
                    item["candidate_sha256"],
                )
                risk_context = item["risk_context"]
                if (
                    type(risk_context) is not dict
                    or set(risk_context)
                    != {
                        "provider_accounts",
                        "bankroll_id",
                        "currency",
                        "measurement_window_start",
                        "measurement_window_end",
                        "proposal_ts",
                    }
                ):
                    raise ValueError("risk_context audit fields are invalid")
                _, evidence_observed = _canonical_timestamp(
                    "intent evidence observed_at",
                    evidence.observed_at,
                )
                _, evidence_cutoff = _canonical_timestamp(
                    "intent evidence causal_cutoff",
                    evidence.causal_cutoff,
                )
                if (
                    evidence_observed > evidence_decision_time
                    or evidence_cutoff > evidence_decision_time
                ):
                    raise ValueError(
                        "intent evidence is from the future"
                    )

                measurement_start = risk_context[
                    "measurement_window_start"
                ]
                measurement_end = risk_context[
                    "measurement_window_end"
                ]
                proposal_ts = risk_context["proposal_ts"]
                if (measurement_start is None) != (
                    measurement_end is None
                ):
                    raise ValueError(
                        "measurement window bounds disagree"
                    )
                proposal_time = None
                if proposal_ts is not None:
                    _, proposal_time = _canonical_timestamp(
                        "intent risk_context proposal_ts",
                        proposal_ts,
                    )
                    if proposal_time > evidence_decision_time:
                        raise ValueError(
                            "intent proposal is from the future"
                        )
                if measurement_start is not None:
                    _, window_start = _canonical_timestamp(
                        "intent risk_context measurement_window_start",
                        measurement_start,
                    )
                    _, window_end = _canonical_timestamp(
                        "intent risk_context measurement_window_end",
                        measurement_end,
                    )
                    if (
                        window_start > window_end
                        or (
                            proposal_time is not None
                            and window_end > proposal_time
                        )
                    ):
                        raise ValueError(
                            "intent measurement window is invalid"
                        )
            except (
                InvalidOperation,
                OpportunityContractError,
                TypeError,
                ValueError,
            ) as exc:
                raise DecisionLedgerIntegrityError(
                    "committed live decision intent execution item is invalid"
                ) from exc
            if (
                not signal_strength.is_finite()
                or opportunity.to_dict() != item["opportunity"]
                or opportunity.opportunity_id != item["opportunity_id"]
                or evidence.to_dict() != item["evidence"]
                or evidence.evidence_sha256 != item["evidence_sha256"]
                or risk_context["bankroll_id"]
                != self.authority.contract.bankroll_id
                or risk_context["currency"] != self.authority.contract.currency
            ):
                raise DecisionLedgerIntegrityError(
                    "committed live decision intent execution item conflicts "
                    "with canonical evidence"
                )
            recomputed_intent_sha256 = _canonical_json_sha256(
                {
                    "schema": "autosport.opportunity_intent",
                    "schema_version": 2,
                    "intent_id": item["intent_id"],
                    "opportunity_id": opportunity.opportunity_id,
                    "opportunity_class": opportunity.strategy_class.value,
                    "opportunity_decision": opportunity.decision.value,
                    "evidence_sha256": evidence.evidence_sha256,
                    "candidate_sha256": item["candidate_sha256"],
                    "signal_strength": str(signal_strength),
                    "strategy_id": item["strategy_id"],
                    "model_id": item["model_id"],
                    "config_sha256": item["config_sha256"],
                }
            )
            if recomputed_intent_sha256 != item["intent_sha256"]:
                raise DecisionLedgerIntegrityError(
                    "committed live decision intent execution hash is invalid"
                )
            canonical_opportunities.append(opportunity)

        if tuple(
            opportunity.strategy_class.value
            for opportunity in canonical_opportunities
        ) != durable_plan.opportunity_classes:
            raise DecisionLedgerIntegrityError(
                "committed live decision opportunity classes conflict with plan"
            )
        if (
            durable_plan.dependency_graph is not None
            and tuple(
                item["candidate_sha256"] for item in intent_items
            )
            != durable_plan.dependency_graph.candidate_sha256s
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision candidate identities conflict with "
                "portfolio dependency graph"
            )

        proven_market_events_by_intent: dict[
            str, tuple[MarketEvent, ...]
        ] = {}
        if progress.market_append_generation is not None:
            if committed_market_history is None:
                raise DecisionLedgerIntegrityError(
                    "committed live decision lacks proven market prefix"
                )
            boundary, age_limit = MarketMirror._decision_boundary(
                as_of=evidence_decision_time,
                max_age=self.max_quote_age,
            )
            committed_snapshot = (
                MarketMirror._decision_view_from_proven_history(
                    committed_market_history,
                    boundary=boundary,
                    max_age=age_limit,
                    source_ids=None,
                    sports=None,
                    event_ids=None,
                    market_ids=None,
                    selection_ids=None,
                )
            )
            if (
                self._market_state_sha256_for_events(
                    committed_snapshot.events
                )
                != progress.market_state_sha256
            ):
                raise DecisionLedgerIntegrityError(
                    "committed live decision market state conflicts with "
                    "proven append prefix"
                )

            decision_visible_events = committed_snapshot.events
            for item, opportunity in zip(
                intent_items,
                canonical_opportunities,
                strict=True,
            ):
                matched_events: list[MarketEvent] = []
                for quote in opportunity.quotes:
                    matching_events = tuple(
                        event
                        for event in decision_visible_events
                        if QuoteRef.from_market_event(
                            event,
                            market_snapshot_hash=(
                                quote.market_snapshot_hash
                            ),
                        )
                        == quote
                    )
                    if len(matching_events) != 1:
                        raise DecisionLedgerIntegrityError(
                            "committed live decision quote is not bound to "
                            "proven market history"
                        )
                    matched_events.append(matching_events[0])
                proven_market_events_by_intent[item["intent_id"]] = tuple(
                    matched_events
                )

                risk_context = item["risk_context"]
                provider_accounts_raw = risk_context["provider_accounts"]
                if type(provider_accounts_raw) is not list:
                    raise DecisionLedgerIntegrityError(
                        "committed live decision provider-account evidence is invalid"
                    )
                provider_accounts: list[tuple[str, str]] = []
                try:
                    for binding_raw in provider_accounts_raw:
                        if (
                            type(binding_raw) is not list
                            or len(binding_raw) != 2
                        ):
                            raise ValueError(
                                "provider account binding is not canonical"
                            )
                        provider_accounts.append(
                            (
                                _canonical_text(
                                    "provider account source_id",
                                    binding_raw[0],
                                ),
                                _canonical_text(
                                    "provider account_id",
                                    binding_raw[1],
                                ),
                            )
                        )
                except (TypeError, ValueError) as exc:
                    raise DecisionLedgerIntegrityError(
                        "committed live decision provider-account evidence is invalid"
                    ) from exc
                canonical_accounts = tuple(provider_accounts)
                source_ids = tuple(
                    source_id for source_id, _ in canonical_accounts
                )
                if (
                    canonical_accounts != tuple(sorted(canonical_accounts))
                    or len(canonical_accounts) != len(set(canonical_accounts))
                    or len(source_ids) != len(set(source_ids))
                    or (
                        canonical_accounts
                        and frozenset(source_ids)
                        != frozenset(
                            event.source_id for event in matched_events
                        )
                    )
                ):
                    raise DecisionLedgerIntegrityError(
                        "committed live decision provider-account evidence is noncanonical"
                    )

                candidate_payload = {
                    "schema": "autosport.risk-candidate.v2",
                    "legs": [
                        {
                            "event_id": quote.event_id,
                            "market_id": quote.market_id,
                            "selection_id": quote.selection_id,
                            "locked_odds": str(quote.decimal_odds),
                        }
                        for quote in sorted(
                            opportunity.quotes,
                            key=lambda value: value.quote_key,
                        )
                    ],
                    "quotes": [
                        event.to_dict()
                        for event in sorted(
                            matched_events,
                            key=lambda value: value.quote_key,
                        )
                    ],
                    "provider_accounts": [
                        {
                            "source_id": source_id,
                            "account_id": account_id,
                        }
                        for source_id, account_id in canonical_accounts
                    ],
                    "bankroll_id": risk_context["bankroll_id"],
                    "currency": risk_context["currency"],
                    "measurement_window_start": risk_context[
                        "measurement_window_start"
                    ],
                    "measurement_window_end": risk_context[
                        "measurement_window_end"
                    ],
                    "proposal_ts": risk_context["proposal_ts"],
                }
                if (
                    _canonical_json_sha256(candidate_payload)
                    != item["candidate_sha256"]
                ):
                    raise DecisionLedgerIntegrityError(
                        "committed live decision risk candidate conflicts with "
                        "proven market/economic evidence"
                    )
        elif committed_market_history is not None:
            raise DecisionLedgerIntegrityError(
                "legacy committed decision unexpectedly supplied market prefix"
            )

        try:
            execution_events = runtime.ledger.events(run_id)
        except PaperExecutionIntegrityError as exc:
            raise DecisionLedgerIntegrityError(
                "committed live decision execution ledger is invalid"
            ) from exc
        reservations = tuple(
            event
            for event in execution_events
            if event.get("event_type") == "RUN_RESERVED"
        )
        scopes = tuple(
            event
            for event in execution_events
            if event.get("event_type")
            == PaperExecutionAdoptionRuntime._EXPOSURE_SCOPE_EVENT_TYPE
        )
        if len(reservations) != 1 or len(scopes) != 1:
            raise DecisionLedgerIntegrityError(
                "committed live decision lacks exact #623 reservation/scope evidence"
            )

        reservation_event = reservations[0]
        scope_event = scopes[0]
        reservation = reservation_event.get("payload")
        expected_reservation_keys = {
            "trigger_id",
            "plan_id",
            "plan_fingerprint",
            "model_fingerprint",
            "started_at",
            "action_ids",
            "observation_evidence_ids",
        }
        if (
            type(reservation) is not dict
            or set(reservation) != expected_reservation_keys
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 reservation is invalid"
            )
        scope_sequence = scope_event.get("sequence")
        reservation_sequence = reservation_event.get("sequence")
        if (
            type(scope_sequence) is not int
            or type(reservation_sequence) is not int
            or scope_sequence >= reservation_sequence
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 scope/reservation chronology is invalid"
            )
        action_ids = reservation.get("action_ids")
        positive_count = sum(stake > 0 for stake in durable_plan.stakes)
        if (
            reservation.get("trigger_id") != progress.decision_id
            or reservation.get("plan_id") != plan_id
            or reservation.get("plan_fingerprint") != plan_fingerprint
            or reservation.get("model_fingerprint") != model_fingerprint
            or reservation.get("started_at") != progress.decision_ts
            or reservation.get("observation_evidence_ids") != {}
            or type(action_ids) is not list
            or len(action_ids) != positive_count
            or len(action_ids) != len(set(action_ids))
            or any(
                type(action_id) is not str or not action_id
                for action_id in action_ids
            )
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 reservation conflicts with "
                "decision evidence"
            )

        attempt_events = tuple(
            event
            for event in execution_events
            if event.get("event_type") == "ATTEMPT_RECORDED"
        )
        completions = tuple(
            event
            for event in execution_events
            if event.get("event_type") == "RUN_COMPLETED"
        )
        allowed_execution_event_types = {
            PaperExecutionAdoptionRuntime._EXPOSURE_SCOPE_EVENT_TYPE,
            "RUN_RESERVED",
            "ATTEMPT_RECORDED",
            "RUN_COMPLETED",
        }
        if (
            len(execution_events) != len(attempt_events) + 3
            or any(
                event.get("event_type") not in allowed_execution_event_types
                for event in execution_events
            )
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 run contains noncanonical events"
            )
        try:
            attempts = tuple(
                PaperLegAttempt.from_dict(event.get("payload"))
                for event in attempt_events
            )
            if tuple(
                attempt.sequence for attempt in attempts
            ) != tuple(range(len(attempts))):
                raise PaperExecutionIntegrityError(
                    "durable attempt events are not in canonical sequence order"
                )
            derived = _derive_run_economics(
                tuple(action_ids),
                attempts,
            )
        except (TypeError, ValueError, PaperExecutionIntegrityError) as exc:
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 attempts are invalid"
            ) from exc
        if len(completions) != 1 or not derived.can_complete:
            raise DecisionLedgerIntegrityError(
                "committed live decision lacks terminal #623 completion"
            )
        completion = completions[0]
        completion_sequence = completion.get("sequence")
        if (
            type(completion_sequence) is not int
            or completion_sequence <= reservation_sequence
            or any(
                type(event.get("sequence")) is not int
                or event["sequence"] <= reservation_sequence
                or event["sequence"] >= completion_sequence
                for event in attempt_events
            )
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 event chronology is invalid"
            )
        completion_payload = completion.get("payload")
        if (
            type(completion_payload) is not dict
            or set(completion_payload)
            != {
                "pending_action_ids",
                "recovery_decision",
                "worst_case_exposure",
            }
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 completion payload is invalid"
            )
        try:
            pending_action_ids = tuple(
                completion_payload["pending_action_ids"]
            )
            recovery_decision = RecoveryDecision(
                completion_payload["recovery_decision"]
            )
            worst_case_exposure = Decimal(
                completion_payload["worst_case_exposure"]
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            InvalidOperation,
        ) as exc:
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 completion economics are invalid"
            ) from exc
        if (
            not worst_case_exposure.is_finite()
            or worst_case_exposure < 0
            or pending_action_ids != derived.pending_action_ids
            or recovery_decision is not derived.recovery_decision
            or worst_case_exposure != derived.worst_case_exposure
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 completion conflicts with "
                "durable attempt economics"
            )

        scope = scopes[0].get("payload")
        if (
            type(scope) is not dict
            or set(scope)
            != {
                "schema",
                "schema_version",
                "plan_id",
                "plan_fingerprint",
                "intent_evidence_sha256",
                "bindings",
                "binding_sha256",
            }
            or scope.get("schema")
            != PaperExecutionAdoptionRuntime._EXPOSURE_SCOPE_SCHEMA
            or scope.get("schema_version") != 1
            or scope.get("plan_id") != plan_id
            or scope.get("plan_fingerprint") != plan_fingerprint
            or scope.get("intent_evidence_sha256")
            != hashlib.sha256(intent_evidence_json.encode("utf-8")).hexdigest()
            or type(scope.get("bindings")) is not list
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 exposure scope conflicts with "
                "decision evidence"
            )
        bindings = scope["bindings"]
        if tuple(
            binding.get("action_id") if type(binding) is dict else None
            for binding in bindings
        ) != tuple(action_ids):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 exposure bindings conflict with "
                "reservation"
            )
        scope_body = dict(scope)
        binding_sha256 = scope_body.pop("binding_sha256")
        if binding_sha256 != _canonical_json_sha256(scope_body):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 exposure scope digest is invalid"
            )

        positive_inputs = tuple(
            (index, item, opportunity, stake)
            for index, (item, opportunity, stake) in enumerate(
                zip(
                    intent_items,
                    canonical_opportunities,
                    durable_plan.stakes,
                    strict=True,
                )
            )
            if stake > 0
        )
        if len(positive_inputs) != len(action_ids):
            raise DecisionLedgerIntegrityError(
                "committed live decision execution inputs conflict with plan"
            )

        actions: list[ExecutionAction] = []
        canonical_bindings: list[PaperExposureBinding] = []
        attempt_by_action = {
            attempt.action_id: attempt for attempt in attempts
        }
        for position, (
            original_index,
            intent_item,
            opportunity,
            stake,
        ) in enumerate(positive_inputs):
            action_id = action_ids[position]
            binding_raw = bindings[position]
            if len(opportunity.quotes) != 1:
                raise DecisionLedgerIntegrityError(
                    "committed live decision positive execution intent is "
                    "not exactly single-leg"
                )
            proven_events = proven_market_events_by_intent.get(
                intent_item["intent_id"],
            )
            if proven_events is not None:
                if len(proven_events) != 1:
                    raise DecisionLedgerIntegrityError(
                        "committed live decision execution quote proof is ambiguous"
                    )
                try:
                    runtime._require_back_compatible_exchange_side(
                        proven_events[0].exchange_side
                    )
                except PaperExecutionAdoptionError as exc:
                    raise DecisionLedgerIntegrityError(
                        "committed live decision execution side lacks "
                        "canonical PAPER authority"
                    ) from exc
            if (
                type(intent_item) is not dict
                or type(binding_raw) is not dict
                or set(binding_raw)
                != {"action_id", "sport", "bankroll_id", "currency"}
            ):
                raise DecisionLedgerIntegrityError(
                    "committed live decision execution input binding is invalid"
                )
            risk_context = intent_item["risk_context"]
            candidate_quotes = tuple(
                quote
                for quote in opportunity.quotes
                if (
                    "paper-action-v1-"
                    + _canonical_json_sha256(
                        {
                            "decision_id": progress.decision_id,
                            "intent_id": intent_item["intent_id"],
                            "intent_sha256": intent_item["intent_sha256"],
                            "quote_market_event_hash": quote.market_event_hash,
                            "stake": str(stake),
                            "index": original_index,
                        }
                    )
                    == action_id
                )
            )
            if len(candidate_quotes) != 1:
                raise DecisionLedgerIntegrityError(
                    "committed live decision #623 action identity is invalid"
                )
            quote = candidate_quotes[0]

            provider_accounts = risk_context.get("provider_accounts")
            if (
                type(provider_accounts) is not list
                or len(provider_accounts) != 1
                or type(provider_accounts[0]) is not list
                or len(provider_accounts[0]) != 2
                or provider_accounts[0][0] != quote.source_id
                or type(provider_accounts[0][1]) is not str
                or not provider_accounts[0][1]
            ):
                raise DecisionLedgerIntegrityError(
                    "committed live decision execution account evidence is invalid"
                )
            account_id = provider_accounts[0][1]

            try:
                quote_clock = quote.source_ts or quote.observed_ts
                _, quote_time = _canonical_timestamp(
                    "execution quote observed time",
                    quote_clock,
                )
                action = ExecutionAction(
                    action_id=action_id,
                    bookmaker_id=quote.source_id,
                    account_id=account_id,
                    event_id=quote.event_id,
                    market_id=quote.market_id,
                    selection_id=quote.selection_id,
                    side="BACK",
                    requested_odds=quote.decimal_odds,
                    requested_stake=stake,
                    quote_id=quote.market_event_hash,
                    quote_observed_at=quote_time.isoformat(
                        timespec="microseconds"
                    ),
                    expires_at=(
                        quote_time + runtime.max_quote_age
                    ).isoformat(timespec="microseconds"),
                )
                binding = PaperExposureBinding(
                    action_id=action_id,
                    sport=quote.sport,
                    bankroll_id=risk_context.get("bankroll_id"),
                    currency=risk_context.get("currency"),
                )
            except (TypeError, ValueError) as exc:
                raise DecisionLedgerIntegrityError(
                    "committed live decision execution action is invalid"
                ) from exc

            attempt = attempt_by_action.get(action_id)
            if attempt is not None and (
                attempt.bookmaker_id != action.bookmaker_id
                or attempt.account_id != action.account_id
                or attempt.event_id != action.event_id
                or attempt.market_id != action.market_id
                or attempt.selection_id != action.selection_id
                or attempt.side != action.side
                or attempt.decision_quote_id != action.quote_id
                or attempt.decision_odds != action.requested_odds
                or attempt.requested_stake != action.requested_stake
            ):
                raise DecisionLedgerIntegrityError(
                    "committed live decision #623 attempt conflicts with "
                    "canonical intent evidence"
                )

            if binding_raw != {
                "action_id": binding.action_id,
                "sport": binding.sport,
                "bankroll_id": binding.bankroll_id,
                "currency": binding.currency,
            }:
                raise DecisionLedgerIntegrityError(
                    "committed live decision #623 exposure binding conflicts "
                    "with canonical intent evidence"
                )
            actions.append(action)
            canonical_bindings.append(binding)

        expected_plan_id = "paper-plan-v1-" + _canonical_json_sha256(
            {
                "decision_id": progress.decision_id,
                "portfolio_plan_sha256": durable_plan.plan_sha256,
                "intent_evidence_json": intent_evidence_json,
                "model_fingerprint": runtime.config.fingerprint,
                "action_ids": action_ids,
            }
        )
        try:
            reconstructed_plan = ExecutionPlan(
                plan_id=expected_plan_id,
                bookmaker_profile_version=(
                    "paper-execution-reality:"
                    f"{runtime.config.model_id}:"
                    f"{runtime.config.model_version}"
                ),
                decision_id=progress.decision_id,
                approval_id="paper-only-no-real-money",
                created_at=durable_plan.decision_ts,
                actions=tuple(actions),
            )
        except (TypeError, ValueError) as exc:
            raise DecisionLedgerIntegrityError(
                "committed live decision execution plan is invalid"
            ) from exc
        if (
            plan_id != expected_plan_id
            or plan_fingerprint != reconstructed_plan.fingerprint
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision execution plan conflicts with "
                "canonical plan/evidence"
            )

        expected_run_id = "paper-exec-v2-" + _canonical_json_sha256(
            {
                "plan_fingerprint": reconstructed_plan.fingerprint,
                "trigger_id": progress.decision_id,
                "model_fingerprint": runtime.config.fingerprint,
            }
        )
        if run_id != expected_run_id:
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 run identity is invalid"
            )
        if (
            scope_event.get("event_key") != f"{run_id}:exposure-scope"
            or reservation_event.get("event_key") != f"{run_id}:reserve"
            or completion.get("event_key") != f"{run_id}:complete"
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision #623 event identity is invalid"
            )
        for event, attempt in zip(
            attempt_events,
            attempts,
            strict=True,
        ):
            if event.get("event_key") != (
                f"{run_id}:attempt:{attempt.sequence}"
            ):
                raise DecisionLedgerIntegrityError(
                    "committed live decision #623 attempt event identity is invalid"
                )
            expected_attempt = _synthetic_attempt(
                run_id=run_id,
                plan=reconstructed_plan,
                action=reconstructed_plan.actions[attempt.sequence],
                sequence=attempt.sequence,
                config=runtime.config,
                started_at=progress.decision_ts,
                suspended=False,
            )
            if attempt != expected_attempt:
                raise DecisionLedgerIntegrityError(
                    "committed live decision #623 attempt conflicts with "
                    "canonical synthetic execution"
                )

        action_by_id = {
            action.action_id: action for action in actions
        }
        binding_by_id = {
            binding.action_id: binding
            for binding in canonical_bindings
        }
        ticket_reason_prefix = (
            "paper execution adoption; "
            f"decision_id={progress.decision_id}; "
            f"run_id={run_id}; "
            f"{PaperExecutionAdoptionRuntime._TICKET_MARKER}"
        )
        claimed_tickets: dict[str, list[object]] = {}
        for ticket in runtime.book.tickets.values():
            if not ticket.strategy_reason.startswith(ticket_reason_prefix):
                continue
            attempt_id = ticket.strategy_reason[
                len(ticket_reason_prefix) :
            ]
            if not attempt_id:
                raise DecisionLedgerIntegrityError(
                    "committed live decision PaperBook has empty #623 "
                    "attempt marker"
                )
            claimed_tickets.setdefault(attempt_id, []).append(ticket)

        expected_ticket_attempt_ids = {
            attempt.attempt_id
            for attempt in attempts
            if (
                self.mode is LiveDecisionMode.PAPER
                and attempt.outcome
                in {
                    PaperAttemptOutcome.ACCEPTED,
                    PaperAttemptOutcome.PARTIAL,
                }
            )
        }
        if (
            set(claimed_tickets) != expected_ticket_attempt_ids
            or any(
                len(values) != 1
                for values in claimed_tickets.values()
            )
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision PaperBook #623 marker set "
                "conflicts with terminal attempts"
            )

        for attempt in attempts:
            marker = (
                f"{PaperExecutionAdoptionRuntime._TICKET_MARKER}"
                f"{attempt.attempt_id}"
            )
            matches = tuple(
                claimed_tickets.get(attempt.attempt_id, ())
            )
            should_materialize = (
                self.mode is LiveDecisionMode.PAPER
                and attempt.outcome
                in {
                    PaperAttemptOutcome.ACCEPTED,
                    PaperAttemptOutcome.PARTIAL,
                }
            )
            if not should_materialize:
                if matches:
                    raise DecisionLedgerIntegrityError(
                        "committed live decision materialized unauthorized "
                        "#623 exposure"
                    )
                continue
            action = action_by_id.get(attempt.action_id)
            binding = binding_by_id.get(attempt.action_id)
            if (
                action is None
                or binding is None
                or len(matches) != 1
                or not runtime._ticket_matches_attempt(
                    ticket=matches[0],
                    attempt=attempt,
                    action=action,
                    binding=binding,
                )
            ):
                raise DecisionLedgerIntegrityError(
                    "committed live decision PaperBook does not bind exact "
                    "#623 execution attempt"
                )
            expected_reason = (
                "paper execution adoption; "
                f"decision_id={progress.decision_id}; "
                f"run_id={run_id}; {marker}"
            )
            if matches[0].strategy_reason != expected_reason:
                raise DecisionLedgerIntegrityError(
                    "committed live decision PaperBook execution reason "
                    "is not canonical"
                )

    def _verify_committed_progress_ledger_binding(
        self,
        progress: _Progress,
        *,
        committed_market_history: tuple[
            tuple[MarketEvent, int], ...
        ]
        | None = None,
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
        try:
            detached_payload = existing.to_dict()["payload"]
            durable_plan = PortfolioPlan.from_dict(detached_payload.get("plan"))
        except (KeyError, TypeError, ValueError) as exc:
            raise DecisionLedgerIntegrityError(
                "committed live decision PortfolioPlan is invalid"
            ) from exc
        if (
            durable_plan.plan_sha256 != progress.plan_sha256
            or durable_plan.decision_ts != progress.decision_ts
            or existing.action != f"LIVE_{durable_plan.action.value.upper()}"
        ):
            raise DecisionLedgerIntegrityError(
                "committed live decision PortfolioPlan conflicts with progress"
            )
        self._verify_committed_execution_binding(
            existing=existing,
            durable_plan=durable_plan,
            progress=progress,
            committed_market_history=committed_market_history,
        )
        payload_version = existing.payload.get("schema_version")
        if payload_version not in {1, 2}:
            raise DecisionLedgerIntegrityError(
                "committed live decision has unsupported schema_version"
            )
        if payload_version == 2:
            expected_payload_keys = {
                "schema",
                "schema_version",
                "loop_id",
                "mode",
                "gate",
                "market_state_sha256",
                "decision_context_sha256",
                "intent_strategy_version_id",
                "intent_model_version_id",
                "intent_provenance_sha256",
                "affected_input_ids",
                "plan_sha256",
                "plan",
                MATERIAL_ACTION_ID_PAYLOAD_KEY,
            }
            if any(stake > 0 for stake in durable_plan.stakes):
                expected_payload_keys.add("paper_execution")
            if set(existing.payload) != expected_payload_keys:
                raise DecisionLedgerIntegrityError(
                    "committed live decision payload schema is noncanonical"
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

    @staticmethod
    def _control_state_sha256(control: _Control | None) -> str | None:
        if control is None:
            return None
        return _canonical_json_sha256(control.to_dict())

    def _control_transition_binding(
        self,
        *,
        previous_state_sha256: str | None,
        candidate: _Control,
        kind: str,
    ) -> str:
        return _canonical_json_sha256(
            {
                "schema": "autosport.live_decision_control_transition",
                "schema_version": 1,
                "kind": kind,
                "loop_id": self.loop_id,
                "previous_state_sha256": previous_state_sha256,
                "intended_state_sha256": self._control_state_sha256(candidate),
                "state": candidate.state.value,
            }
        )

    @staticmethod
    def _control_tx_id(binding_sha256: str) -> str:
        return f"live-control-{binding_sha256}"

    def _read_control_file(self) -> _Control | None:
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

    def _load_control(self) -> _Control | None:
        control = self._read_control_file()
        if control is not None and control.loop_id != self.loop_id:
            raise LiveDecisionProgressError(
                "persisted live control belongs to a different loop_id"
            )
        observed = self._control_state_sha256(control)
        try:
            history = self._control_authority.read_history()
            if not history:
                if control is None:
                    return None
                binding = self._control_transition_binding(
                    previous_state_sha256=None,
                    candidate=control,
                    kind="BOOTSTRAP",
                )
                tx_id = self._control_tx_id(binding)
                self._control_authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                self._control_authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                return control

            pending = history[-1] if history[-1].phase is AuthorityPhase.PREPARE else None
            if pending is not None and observed == pending.intended_state_sha256:
                if control is None:
                    raise LiveDecisionProgressError(
                        "prepared live control authority has no durable control bytes"
                    )
                matched: tuple[str, str] | None = None
                for kind in ("TRANSITION", "BOOTSTRAP"):
                    candidate_binding = self._control_transition_binding(
                        previous_state_sha256=pending.previous_committed_state_sha256,
                        candidate=control,
                        kind=kind,
                    )
                    candidate_tx_id = self._control_tx_id(candidate_binding)
                    if (
                        candidate_tx_id == pending.tx_id
                        and candidate_binding == pending.semantic_binding_sha256
                    ):
                        matched = (candidate_tx_id, candidate_binding)
                        break
                if matched is None:
                    raise LiveDecisionProgressError(
                        "prepared live control authority conflicts with durable control semantics"
                    )
                tx_id, binding = matched
                self._control_authority.recover(
                    observed_state_sha256=observed,
                    tx_id=tx_id,
                    semantic_binding_sha256=binding,
                )
            else:
                self._control_authority.recover(
                    observed_state_sha256=observed,
                )
            return control
        except MonotonicWorkspaceAuthorityError as exc:
            raise LiveDecisionProgressError(
                "live decision control failed monotonic rollback/recovery verification"
            ) from exc

    def _persist_control(self, state: LiveControlState) -> None:
        self._assert_canonical_persistence_authority()
        candidate = _Control(self.loop_id, state)
        with WorkspaceEconomicLock(self._workspace_authority):
            self._assert_canonical_persistence_authority()
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
            if durable == candidate:
                self._control = candidate
                return

            observed = self._control_state_sha256(durable)
            intended = self._control_state_sha256(candidate)
            assert intended is not None
            binding = self._control_transition_binding(
                previous_state_sha256=observed,
                candidate=candidate,
                kind="TRANSITION",
            )
            tx_id = self._control_tx_id(binding)
            try:
                self._control_authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                atomic_write_json(self.control_path, candidate.to_dict())
                published = self._read_control_file()
                if published != candidate:
                    raise LiveDecisionProgressError(
                        "live control publication changed before monotonic commit"
                    )
                self._control_authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise LiveDecisionProgressError(
                    "live decision control monotonic publication failed"
                ) from exc
        self._control = candidate

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
