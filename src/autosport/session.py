from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .agents import AgentContext, AgentOrchestrator
from .dataset import ReplayDataset
from .decision_ledger import JsonlDecisionLedger
from .domain import MarketEvent
from .evaluation import EvaluationSummary, evaluate
from .ingestion import IngestionEngine, IngestionStats
from .ingestion_health import IngestionPolicy, SourceHealthState, SourceHealthStore
from .integrity import ensure_durable_file, sha256_file
from .market_bus import MarketEventBus
from .paper import PaperBook
from .portfolio import PortfolioEngine, PortfolioReport
from .providers import MarketProvider
from .replay import ReplayEngine, ReplayRun
from .run_registry import RunRegistry, UnresolvedExperimentError
from .run_transaction import RunTransaction
from .settlement import SettlementEngine
from .storage import SQLiteMarketStore
from .strategies import StrategySpec, build_strategy_agents, strategy_spec
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
    ) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        # Bind the experiment identity to an actual runtime implementation now,
        # before durable state is opened. Arbitrary labels must never appear in
        # evaluation evidence for a different strategy implementation.
        self.strategy: StrategySpec = strategy_spec(strategy_id)
        self.strategy_id = self.strategy.strategy_id
        self.store = SQLiteMarketStore(self.workspace / "market.db")
        self.source_health = SourceHealthStore(self.workspace / "source_health.json")
        self.book_path = self.workspace / "paper_book.json"
        self.book = PaperBook.load(self.book_path) if self.book_path.exists() else PaperBook(initial_bankroll)
        self.ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
        self.registry = RunRegistry(self.workspace / "run_registry.json")
        self.portfolio_engine = PortfolioEngine()

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
        return AgentOrchestrator(build_strategy_agents(self.strategy_id), context)

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
            return self._run_dataset_locked(dataset, speed=speed, allow_repeat=allow_repeat)

    def _run_dataset_locked(
        self,
        dataset: ReplayDataset,
        *,
        speed: float = 0.0,
        allow_repeat: bool = False,
    ) -> SessionResult:
        self._ensure_canonical_economic_base()
        base_book_hash = sha256_file(self.book_path)
        base_ledger_hash = sha256_file(self.ledger.path)

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

        working_book = PaperBook.load(self.book_path)
        staged_ledger = JsonlDecisionLedger(transaction.run_ledger_path)
        orchestrator = self._runtime(run_id, book=working_book, ledger=staged_ledger)
        engine = ReplayEngine(dataset.load_market_events())

        def consume(event) -> None:
            self.store.append(event)
            orchestrator.on_market_event(event)

        replay = engine.run(consume, speed=speed, run_id=run_id)
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
        return result

    def _ensure_canonical_economic_base(self) -> None:
        if self.registry.in_progress():
            raise UnresolvedExperimentError(
                "Workspace has an unresolved economic run; repair it before starting another paper experiment."
            )
        self.book.save(self.book_path)
        ensure_durable_file(self.ledger.path)

    def _run_summary_payload(
        self,
        dataset: ReplayDataset,
        result: SessionResult,
    ) -> dict:
        return {
            "schema_version": 2,
            "dataset_name": dataset.name,
            "sport": dataset.sport,
            "dataset_schema_version": dataset.schema_version,
            "historical_import_identity": dataset.import_identity,
            "dataset_governance": asdict(dataset.governance) if dataset.governance is not None else None,
            "strategy_id": self.strategy_id,
            "strategy_runtime": {
                "strategy_id": self.strategy.strategy_id,
                "label": self.strategy.label,
                "agent_names": list(self.strategy.agent_names),
                "opens_paper_tickets": self.strategy.opens_paper_tickets,
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
        try:
            # Never let teardown overwrite a partially committed transaction with
            # stale in-memory state. Recovery owns any unresolved economic commit.
            if not self.registry.in_progress():
                self.book.save(self.book_path)
        finally:
            self.store.close()
