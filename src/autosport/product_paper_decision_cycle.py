from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock
from typing import Callable

from .causal_collector import SyncState
from .decision_ledger import EconomicDecisionAuthority, JsonlDecisionLedger
from .live_decision_loop import (
    LiveCycleResult,
    LiveDecisionMode,
    LiveIntentFactory,
    LiveIntentProvenance,
    LiveLoopBounds,
    PersistentLiveDecisionLoop,
)
from .paper import PaperBook
from .paper_execution_adoption import PaperExecutionAdoptionRuntime
from .paper_execution_reality import PaperExecutionLedger, PaperExecutionModelConfig
from .product_runtime import AutonomousProductRuntime, ProductCompositionError
from .scientific_registry import ScientificRegistry
from .continuous_session import (
    ContinuousSessionStatus,
    ContinuousTickResult,
    SessionState,
)
from .workspace_lock import WorkspaceEconomicLock


class ProductPaperDecisionCycleError(ProductCompositionError):
    """Raised when the supported product cannot safely enter one PAPER decision cycle."""


_Selector = tuple[str, ...] | None


def _canonical_selector(value: _Selector, field: str) -> _Selector:
    if value is None:
        return None
    if type(value) is not tuple or not value:
        raise ValueError(f"{field} must be a non-empty canonical tuple or None")
    if any(
        type(item) is not str or not item or item.strip() != item
        for item in value
    ):
        raise ValueError(f"{field} must contain non-empty trimmed strings")
    if len(set(value)) != len(value) or value != tuple(sorted(value)):
        raise ValueError(f"{field} must be sorted and unique")
    return value


@dataclass(frozen=True, slots=True)
class ProductDecisionInput:
    """One durable live-decision dependency registration for supported product PAPER."""

    input_id: str
    source_ids: _Selector = None
    sports: _Selector = None
    event_ids: _Selector = None
    market_ids: _Selector = None
    selection_ids: _Selector = None

    def __post_init__(self) -> None:
        if (
            type(self.input_id) is not str
            or not self.input_id
            or self.input_id.strip() != self.input_id
        ):
            raise ValueError("input_id must be non-empty trimmed text")
        for field in (
            "source_ids",
            "sports",
            "event_ids",
            "market_ids",
            "selection_ids",
        ):
            object.__setattr__(
                self,
                field,
                _canonical_selector(getattr(self, field), field),
            )

    def register(self, loop: PersistentLiveDecisionLoop) -> None:
        if not isinstance(loop, PersistentLiveDecisionLoop):
            raise TypeError("loop must be PersistentLiveDecisionLoop")
        loop.register_input(
            self.input_id,
            source_ids=self.source_ids,
            sports=self.sports,
            event_ids=self.event_ids,
            market_ids=self.market_ids,
            selection_ids=self.selection_ids,
        )


@dataclass(frozen=True, slots=True)
class ProductPaperDecisionTickResult:
    product_tick: ContinuousTickResult
    decision: LiveCycleResult | None
    skipped_reason: str | None

    def __post_init__(self) -> None:
        if (self.decision is None) == (self.skipped_reason is None):
            raise ValueError(
                "exactly one of decision or skipped_reason must describe the decision phase"
            )


class ProductPaperDecisionCycle:
    """Compose one product tick with one bounded canonical PAPER decision cycle.

    This is deliberately stacked on the runtime-wide lease authority introduced by
    reliability.product-runtime-process-lease-v1. The supplied AutonomousProductRuntime
    remains the sole provider/network acquisition owner. After its normal collector /
    lifecycle / settlement tick completes, this composition reconstructs decision state
    from the canonical workspace files and gives PersistentLiveDecisionLoop a no-I/O
    observation runner. That avoids both a second provider poller and a long-lived stale
    in-memory PaperBook after settlement.

    The class does not widen provider-write, real-money, settlement, learning, or
    campaign-admission authority. It also does not make arbitrary non-product workspace
    writers safe to run concurrently; supported operation requires the product runtime's
    exclusive process lease plus the existing economic-writer locks.
    """

    def __init__(
        self,
        runtime: AutonomousProductRuntime,
        *,
        loop_id: str,
        authority: EconomicDecisionAuthority,
        intent_factory: LiveIntentFactory,
        scientific_registry: ScientificRegistry,
        execution_config: PaperExecutionModelConfig,
        max_quote_age: timedelta,
        inputs: tuple[ProductDecisionInput, ...],
        bounds: LiveLoopBounds | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(runtime) is not AutonomousProductRuntime:
            raise TypeError("runtime must be the canonical AutonomousProductRuntime")
        if type(loop_id) is not str or not loop_id or loop_id.strip() != loop_id:
            raise ValueError("loop_id must be non-empty trimmed text")
        if not isinstance(authority, EconomicDecisionAuthority):
            raise TypeError("authority must be EconomicDecisionAuthority")
        if not callable(intent_factory):
            raise TypeError("intent_factory must be callable")
        strategy_version_id = getattr(intent_factory, "strategy_version_id", None)
        if (
            type(strategy_version_id) is not str
            or not strategy_version_id
            or strategy_version_id.strip() != strategy_version_id
        ):
            raise TypeError(
                "intent_factory must expose canonical strategy_version_id"
            )
        if not isinstance(scientific_registry, ScientificRegistry):
            raise TypeError("scientific_registry must be ScientificRegistry")
        if not isinstance(execution_config, PaperExecutionModelConfig):
            raise TypeError("execution_config must be PaperExecutionModelConfig")
        if not isinstance(max_quote_age, timedelta) or max_quote_age <= timedelta(0):
            raise ValueError("max_quote_age must be a positive timedelta")
        if max_quote_age > timedelta(milliseconds=execution_config.max_quote_age_ms):
            raise ValueError(
                "max_quote_age cannot exceed the PAPER execution model freshness bound"
            )
        max_quote_age_seconds = (
            Decimal(max_quote_age.days * 86400 + max_quote_age.seconds)
            + Decimal(max_quote_age.microseconds) / Decimal(1_000_000)
        )
        if max_quote_age_seconds > authority.contract.max_quote_age_seconds:
            raise ValueError(
                "max_quote_age cannot exceed EconomicGoalContract.max_quote_age_seconds"
            )
        if type(inputs) is not tuple or not inputs:
            raise ValueError("inputs must be a non-empty tuple")
        if any(type(item) is not ProductDecisionInput for item in inputs):
            raise TypeError("inputs must contain ProductDecisionInput values")
        input_ids = tuple(item.input_id for item in inputs)
        if len(set(input_ids)) != len(input_ids):
            raise ValueError("inputs must not contain duplicate input_id values")
        if bounds is not None and not isinstance(bounds, LiveLoopBounds):
            raise TypeError("bounds must be LiveLoopBounds or None")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or None")

        workspace = Path(runtime.workspace)
        registry_path = Path(scientific_registry.path)
        if registry_path.parent.resolve() != workspace.resolve():
            raise ProductPaperDecisionCycleError(
                "scientific registry must belong to the product runtime workspace"
            )

        self.runtime = runtime
        self.loop_id = loop_id
        self.authority = authority
        self.intent_factory = intent_factory
        self.scientific_registry = scientific_registry
        self.execution_config = execution_config
        self.max_quote_age = max_quote_age
        self.inputs = inputs
        self.bounds = bounds
        self.clock = clock
        self._cycle_lock = Lock()

    @property
    def workspace(self) -> Path:
        return Path(self.runtime.workspace)

    def _require_running_runtime(self) -> None:
        try:
            status = self.runtime.status()
        except Exception as exc:
            raise ProductPaperDecisionCycleError(
                "product runtime authority/lifecycle cannot be verified"
            ) from exc
        if status.state is not SessionState.RUNNING:
            raise ProductPaperDecisionCycleError(
                "PAPER decision cycle requires the canonical product runtime to be running"
            )

    @staticmethod
    def _decision_skip_reason(
        product_tick: ContinuousTickResult,
        status: ContinuousSessionStatus,
    ) -> str | None:
        if getattr(product_tick, "source_provider_unavailable", None) is True:
            return "source_provider_unavailable"
        if getattr(product_tick, "invalidation_backlog", None) is True:
            return "market_invalidation_backlog"
        if getattr(status, "source_provider_unavailable", None) is True:
            return "source_provider_unavailable"
        if getattr(status, "source_sync_state", None) == SyncState.RETRY_REQUIRED.value:
            return "source_retry_required"
        unresolved = getattr(status, "source_unresolved_gap_delta_ids", None)
        if unresolved:
            return "source_gap_unresolved"
        if getattr(status, "source_state_projection_backlog", None) is True:
            return "source_state_projection_backlog"
        if getattr(status, "invalidation_full_refresh_required", None) is True:
            return "market_full_refresh_pending"
        pending = getattr(status, "invalidation_pending_count", None)
        if isinstance(pending, int) and not isinstance(pending, bool) and pending > 0:
            return "market_invalidation_pending"
        return None

    def _initial_bankroll(self) -> Decimal:
        try:
            value = Decimal(self.runtime.manifest.initial_bankroll)
        except (InvalidOperation, ValueError) as exc:
            raise ProductPaperDecisionCycleError(
                "product manifest initial bankroll is not an exact Decimal"
            ) from exc
        if not value.is_finite() or value <= 0:
            raise ProductPaperDecisionCycleError(
                "product manifest initial bankroll must be positive and finite"
            )
        return value

    def _load_current_book(self) -> PaperBook:
        path = self.workspace / "paper_book.json"
        execution_path = self.workspace / "paper-execution.jsonl"
        execution_anchor_path = execution_path.with_name(
            execution_path.name + ".anchor.json"
        )
        execution_writer_lock_path = execution_path.with_name(
            execution_path.name + ".writer.lock"
        )
        initial_bankroll = self._initial_bankroll()
        with WorkspaceEconomicLock(self.workspace):
            if path.exists():
                book = PaperBook.load(path)
            else:
                if execution_writer_lock_path.exists():
                    raise ProductPaperDecisionCycleError(
                        "missing durable PaperBook cannot be recreated while PAPER "
                        "execution ownership is unresolved"
                    )
                if execution_path.exists() or execution_anchor_path.exists():
                    try:
                        prior_execution_events = PaperExecutionLedger(
                            execution_path
                        ).events()
                    except Exception as exc:
                        raise ProductPaperDecisionCycleError(
                            "missing durable PaperBook cannot be recreated because "
                            "PAPER execution history cannot be verified"
                        ) from exc
                    if prior_execution_events:
                        raise ProductPaperDecisionCycleError(
                            "missing durable PaperBook conflicts with existing PAPER "
                            "execution history; recovery is required"
                        )
                book = PaperBook(initial_bankroll)
                book.save(path)
        if book.initial_bankroll != initial_bankroll:
            raise ProductPaperDecisionCycleError(
                "durable PaperBook initial bankroll conflicts with product composition"
            )
        return book

    @staticmethod
    def _no_provider_observation(_updates: object) -> None:
        """Explicitly perform zero provider/network acquisition in the decision phase."""

        return None

    def _run_decision_cycle(self) -> LiveCycleResult:
        self._require_running_runtime()
        provenance_now = (
            self.clock() if self.clock is not None else datetime.now(timezone.utc)
        )
        LiveIntentProvenance.from_registry(
            self.scientific_registry,
            self.intent_factory.strategy_version_id,
            as_of=provenance_now,
        )
        book = self._load_current_book()
        execution_ledger = PaperExecutionLedger(
            self.workspace / "paper-execution.jsonl"
        )
        execution = PaperExecutionAdoptionRuntime(
            book=book,
            ledger=execution_ledger,
            config=self.execution_config,
            max_quote_age=self.max_quote_age,
            paper_book_path=self.workspace / "paper_book.json",
        )
        decision_ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
        loop = PersistentLiveDecisionLoop(
            self.workspace,
            loop_id=self.loop_id,
            mode=LiveDecisionMode.PAPER,
            book=book,
            authority=self.authority,
            intent_factory=self.intent_factory,
            scientific_registry=self.scientific_registry,
            decision_ledger=decision_ledger,
            paper_execution=execution,
            max_quote_age=self.max_quote_age,
            bounds=self.bounds,
            clock=self.clock,
            observation_runner=self._no_provider_observation,
        )
        try:
            for decision_input in self.inputs:
                decision_input.register(loop)
            result = loop.run_cycle()
        finally:
            loop.close()
        self._require_running_runtime()
        return result

    def tick(self) -> ProductPaperDecisionTickResult:
        """Run collector/lifecycle/settlement first, then at most one PAPER decision."""

        if not self._cycle_lock.acquire(blocking=False):
            raise ProductPaperDecisionCycleError(
                "PAPER decision composition already has a product cycle in progress"
            )
        try:
            self._require_running_runtime()
            product_tick = self.runtime.tick()
            status = self.runtime.status()
            skip_reason = self._decision_skip_reason(product_tick, status)
            if skip_reason is not None:
                return ProductPaperDecisionTickResult(
                    product_tick=product_tick,
                    decision=None,
                    skipped_reason=skip_reason,
                )
            decision = self._run_decision_cycle()
            return ProductPaperDecisionTickResult(
                product_tick=product_tick,
                decision=decision,
                skipped_reason=None,
            )
        finally:
            self._cycle_lock.release()
