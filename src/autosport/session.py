from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .agents import AgentContext, AgentOrchestrator
from .dataset import ReplayDataset
from .decision_ledger import JsonlDecisionLedger, VerifiedDecisionLedgerSnapshot
from .domain import MarketEvent
from .evaluation import EvaluationSummary, evaluate
from .ingestion import IngestionEngine, IngestionStats
from .ingestion_health import IngestionPolicy, SourceHealthState, SourceHealthStore
from .integrity import ensure_durable_file, sha256_file
from .market_bus import MarketEventBus
from .paper import PaperBook
from .portfolio import PortfolioEngine, PortfolioReport
from .price_truth import market_price_truth_from_events
from .providers import MarketProvider
from .replay import ReplayEngine, ReplayRun
from .research_strategy import ResearchStrategyPlan
from .run_registry import MixedStrategyWorkspaceError, RunRegistry, UnresolvedExperimentError
from .run_transaction import RunTransaction
from .settlement import SettlementEngine
from .storage import SQLiteMarketStore
from .strategies import (
    StrategySpec,
    build_strategy_agents,
    experiment_strategy_id,
    validate_strategy_configuration,
)
from .workspace_lock import WorkspaceEconomicLock


@dataclass(frozen=True, slots=True)
class SessionResult:
    replay: ReplayRun
    settled_ticket_ids: tuple[str, ...]
    balance: Decimal
    evaluation: EvaluationSummary
    portfolio: PortfolioReport
    experiment_key: str
    result_path: str


@dataclass(frozen=True, slots=True)
class ObservationResult:
    stats: IngestionStats
    health: SourceHealthState
    current_quotes: tuple[MarketEvent, ...]


class AutosportSession:
    """V1 runtime for causal replay, paper simulation and read-only market observation."""

    def __init__(
        self,
        workspace: str | Path,
        initial_bankroll: Decimal | str = "10000",
        strategy_id: str = "baseline-v1",
        research_plan: ResearchStrategyPlan | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        # Bind experiment identity to an actual runtime implementation and, for
        # research replay, the exact canonical plan hash before durable state opens.
        self.strategy: StrategySpec = validate_strategy_configuration(strategy_id, research_plan)
        self.research_plan = research_plan
        self.strategy_id = experiment_strategy_id(strategy_id, research_plan)
        self.store = SQLiteMarketStore(self.workspace / "market.db")
        try:
            self.source_health = SourceHealthStore(self.workspace / "source_health.json")
            self.book_path = self.workspace / "paper_book.json"
            self.book = PaperBook.load(self.book_path) if self.book_path.exists() else PaperBook(initial_bankroll)
            self.ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
            self.registry = RunRegistry.initialize_pristine(self.workspace / "run_registry.json")
            self.portfolio_engine = PortfolioEngine()
        except BaseException as initialization_error:
            # SQLiteMarketStore owns an OS file handle after construction. If any
            # later durable/session component fails to initialize, the partially
            # constructed session must make a best-effort close without replacing
            # the primary initialization failure with a secondary cleanup failure.
            try:
                self.store.close()
            except BaseException as cleanup_error:
                initialization_error.add_note(
                    "SQLiteMarketStore cleanup also failed during session initialization: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

    def _runtime(
        self,
        run_id: str,
        *,
        book: PaperBook | None = None,
        ledger: JsonlDecisionLedger | None = None,
    ) -> AgentOrchestrator:
        context = AgentContext(
            book or self.book,
            replay_run_id=run_id,
            decision_ledger=ledger or self.ledger,
        )
        return AgentOrchestrator(
            build_strategy_agents(
                self.strategy.strategy_id,
                research_plan=self.research_plan,
            ),
            context,
        )

    def observe_provider_once(
        self,
        provider: MarketProvider,
        *,
        max_items: int = 1000,
        policy: IngestionPolicy | None = None,
    ) -> ObservationResult:
        """Acquire one bounded read-only provider snapshot into the canonical Market Store."""
        engine = IngestionEngine(
            MarketEventBus(self.store),
            policy=policy,
            health_store=self.source_health,
        )
        stats = engine.poll_once(provider, max_items=max_items)
        current = tuple(
            sorted(
                (event for event in self.store.current().values() if event.source_id == provider.source_id),
                key=lambda event: (event.event_id, event.market_id, event.selection_id),
            )
        )
        return ObservationResult(stats, self.source_health.get(provider.source_id), current)

    def run_dataset(self, dataset: ReplayDataset, speed: float = 0.0, allow_repeat: bool = False) -> SessionResult:
        with WorkspaceEconomicLock(self.workspace):
            # Research-plan market binding is deterministic from the sealed causal
            # stream, so reject a stale/forged plan before registry/PaperBook mutation.
            if self.research_plan is not None:
                self.research_plan.preflight(dataset.load_market_events())
            return self._run_dataset_locked(dataset, speed=speed, allow_repeat=allow_repeat)

    def _run_dataset_locked(
        self,
        dataset: ReplayDataset,
        *,
        speed: float = 0.0,
        allow_repeat: bool = False,
    ) -> SessionResult:
        base_ledger_snapshot = self._ensure_canonical_economic_base()
        base_book_hash = sha256_file(self.book_path)
        base_ledger_hash = base_ledger_snapshot.sha256

        run_id = str(uuid.uuid4())
        experiment_key = self.registry.begin(
            dataset.market_sha256,
            dataset.results_sha256,
            self.strategy_id,
            run_id,
            allow_repeat=allow_repeat,
            base_paper_book_sha256=base_book_hash,
            base_decision_ledger_sha256=base_ledger_hash,
        )
        try:
            transaction = RunTransaction.start(
                self.workspace,
                run_id=run_id,
                experiment_key=experiment_key,
                market_sha256=dataset.market_sha256,
                results_sha256=dataset.results_sha256,
                strategy_id=self.strategy_id,
                base_paper_book_sha256=base_book_hash,
                base_decision_ledger_sha256=base_ledger_hash,
            )
        except Exception:
            # No economic mutation occurs before the transaction object exists.
            # Do not turn an ordinary start failure into an in-progress workspace
            # that falsely requires crash recovery.
            self.registry.abort_uncommitted(
                experiment_key,
                reason="transaction start failed before durable precommit",
                paper_book_sha256=sha256_file(self.book_path),
                decision_ledger_sha256=self.ledger.verified_snapshot().sha256,
            )
            raise

        try:
            working_book = PaperBook.load(self.book_path)
            staged_ledger = JsonlDecisionLedger(transaction.run_ledger_path)
            orchestrator = self._runtime(run_id, book=working_book, ledger=staged_ledger)
            engine = ReplayEngine(dataset.load_market_events())

            def consume(event) -> None:
                self.store.append(event)
                orchestrator.on_market_event(event)

            replay = engine.run(consume, speed=speed, run_id=run_id)
            # Fail closed on any planned causal decision that did not execute before
            # loading sealed outcome facts into settlement.
            orchestrator.finalize_replay()
            settlement = SettlementEngine()
            settlement.record(dataset.load_results_after_replay())
            settled = tuple(settlement.settle_ready(working_book))
            evaluation = evaluate(working_book)
            portfolio = self.portfolio_engine.analyse(list(working_book.tickets.values()))
            destination = self.workspace / f"run-{replay.run_id}.json"
            result = SessionResult(
                replay,
                settled,
                working_book.balance,
                evaluation,
                portfolio,
                experiment_key,
                str(destination),
            )

            transaction.stage_outputs(working_book, self.ledger.path)
        except Exception:
            # A normal strategy/replay/staging failure before PRECOMMIT is not an
            # ambiguous crash. Recovery must first prove canonical PaperBook and
            # Decision Ledger are still exactly BASE; only then may the registry
            # record an explicit aborted retry.
            recovery = RunTransaction.recover(
                self.workspace,
                run_id=run_id,
                registry_item=self.registry.get(experiment_key),
                experiment_key=experiment_key,
            )
            if recovery.disposition != "aborted_uncommitted":
                raise RuntimeError(
                    "precommit failure unexpectedly crossed the durable commit boundary"
                )
            self.registry.abort_uncommitted(
                experiment_key,
                reason=(
                    "strategy/replay/staging failed before durable precommit; "
                    "canonical economic state remained BASE"
                ),
                paper_book_sha256=sha256_file(self.book_path),
                decision_ledger_sha256=self.ledger.verified_snapshot().sha256,
            )
            raise

        # After durable PRECOMMIT begins, failures deliberately remain unresolved.
        # Recovery owns deciding whether BASE or NEW is canonical and must never
        # downgrade an uncertain commit into an ordinary strategy rejection.
        summary = transaction.precommit(self._run_summary_payload(dataset, result))
        transaction.commit()

        # From this point canonical PaperBook is NEW. Keep in-memory state aligned
        # before touching the registry so finally/close can never rewrite OLD state.
        self.book = working_book
        self.registry.complete(
            experiment_key,
            str(destination),
            paper_book_sha256=str(summary["paper_book_sha256"]),
            decision_ledger_sha256=str(summary["decision_ledger_sha256"]),
        )
        transaction.mark_registry_completed()
        return result

    def _ensure_canonical_economic_base(self) -> VerifiedDecisionLedgerSnapshot:
        if self.registry.in_progress():
            raise UnresolvedExperimentError(
                "Workspace has an unresolved economic run; repair it before starting another paper experiment."
            )
        prior_strategy_ids = self.registry.strategy_ids()
        foreign_strategy_ids = tuple(
            value for value in prior_strategy_ids if value != self.strategy_id
        )
        if foreign_strategy_ids:
            raise MixedStrategyWorkspaceError(
                "Workspace already contains economic runs for another strategy identity "
                f"({', '.join(foreign_strategy_ids)}). Use a separate workspace per strategy/plan "
                "so PaperBook, decision-ledger, portfolio and evaluation evidence cannot be mixed."
            )
        # Validate the append-only audit evidence under the economic lock and bind
        # its semantic proof to the exact bytes whose hash becomes transaction BASE.
        ensure_durable_file(self.ledger.path)
        ledger_snapshot = self.ledger.verified_snapshot()
        # Session construction happens outside WorkspaceEconomicLock. Another process
        # or session may therefore have committed a newer canonical PaperBook while
        # this instance was waiting for the lock. Refresh under the lock instead of
        # writing a stale in-memory snapshot over newer economic state.
        if self.book_path.exists():
            self.book = PaperBook.load(self.book_path)
        else:
            self.book.save(self.book_path)
        return ledger_snapshot

    def _run_summary_payload(
        self,
        dataset: ReplayDataset,
        result: SessionResult,
    ) -> dict:
        market_price_truth = market_price_truth_from_events(dataset.load_market_events())
        return {
            "schema_version": 2,
            "dataset_name": dataset.name,
            "sport": dataset.sport,
            "dataset_schema_version": dataset.schema_version,
            "historical_import_identity": dataset.import_identity,
            "dataset_governance": asdict(dataset.governance) if dataset.governance is not None else None,
            "market_price_truth": market_price_truth.to_dict(),
            "strategy_id": self.strategy_id,
            "strategy_runtime": {
                "strategy_id": self.strategy_id,
                "canonical_strategy_id": self.strategy.strategy_id,
                "label": self.strategy.label,
                "agent_names": list(self.strategy.agent_names),
                "opens_paper_tickets": self.strategy.opens_paper_tickets,
                "research_plan_sha256": (
                    self.research_plan.source_sha256 if self.research_plan is not None else None
                ),
            },
            "experiment_key": result.experiment_key,
            "market_sha256": dataset.market_sha256,
            "sealed_results_sha256": dataset.results_sha256,
            "run_id": result.replay.run_id,
            "event_count": result.replay.event_count,
            "replay_dataset_hash": result.replay.dataset_hash,
            "settled_ticket_ids": list(result.settled_ticket_ids),
            "balance": str(result.balance),
            "evaluation": {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(result.evaluation).items()
            },
            "portfolio": {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(result.portfolio).items()
            },
            "real_money_execution": False,
        }

    def close(self) -> None:
        # Canonical PaperBook persistence is owned by the workspace-locked run
        # transaction path. Teardown must never write an unlocked in-memory snapshot:
        # this session may have been constructed before another process committed a
        # newer canonical book, and saving here would silently roll that commit back.
        self.store.close()