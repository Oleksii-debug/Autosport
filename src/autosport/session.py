from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .agents import AgentContext, AgentOrchestrator, MarketMirrorAgent, PaperBaselineAgent
from .dataset import ReplayDataset
from .decision_ledger import JsonlDecisionLedger
from .domain import MarketEvent
from .evaluation import EvaluationSummary, evaluate
from .ingestion import IngestionEngine, IngestionStats
from .ingestion_health import IngestionPolicy, SourceHealthState, SourceHealthStore
from .integrity import atomic_write_json, sha256_file
from .market_bus import MarketEventBus
from .paper import PaperBook
from .portfolio import PortfolioEngine, PortfolioReport
from .providers import MarketProvider
from .replay import ReplayEngine, ReplayRun
from .run_registry import RunRegistry
from .settlement import SettlementEngine
from .storage import SQLiteMarketStore


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
        self.strategy_id = strategy_id
        self.store = SQLiteMarketStore(self.workspace / "market.db")
        self.source_health = SourceHealthStore(self.workspace / "source_health.json")
        self.book_path = self.workspace / "paper_book.json"
        self.book = PaperBook.load(self.book_path) if self.book_path.exists() else PaperBook(initial_bankroll)
        self.ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
        self.registry = RunRegistry(self.workspace / "run_registry.json")
        self.portfolio_engine = PortfolioEngine()

    def _runtime(self, run_id: str) -> AgentOrchestrator:
        context = AgentContext(self.book, replay_run_id=run_id, decision_ledger=self.ledger)
        return AgentOrchestrator([MarketMirrorAgent(), PaperBaselineAgent("50")], context)

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
        run_id = str(uuid.uuid4())
        experiment_key = self.registry.begin(
            dataset.market_sha256,
            dataset.results_sha256,
            self.strategy_id,
            run_id,
            allow_repeat=allow_repeat,
        )
        orchestrator = self._runtime(run_id)
        engine = ReplayEngine(dataset.load_market_events())

        def consume(event) -> None:
            self.store.append(event)
            orchestrator.on_market_event(event)

        replay = engine.run(consume, speed=speed, run_id=run_id)
        settlement = SettlementEngine()
        settlement.record(dataset.load_results_after_replay())
        settled = tuple(settlement.settle_ready(self.book))
        self.book.save(self.book_path)
        paper_book_sha256 = sha256_file(self.book_path)
        evaluation = evaluate(self.book)
        portfolio = self.portfolio_engine.analyse(list(self.book.tickets.values()))
        destination = self.workspace / f"run-{replay.run_id}.json"
        result = SessionResult(
            replay,
            settled,
            self.book.balance,
            evaluation,
            portfolio,
            experiment_key,
            str(destination),
        )
        self._write_run_summary(dataset, result, destination, paper_book_sha256)
        self.registry.complete(experiment_key, str(destination))
        return result

    def _write_run_summary(
        self,
        dataset: ReplayDataset,
        result: SessionResult,
        destination: Path,
        paper_book_sha256: str,
    ) -> None:
        payload = {
            "schema_version": 2,
            "dataset_name": dataset.name,
            "sport": dataset.sport,
            "strategy_id": self.strategy_id,
            "experiment_key": result.experiment_key,
            "market_sha256": dataset.market_sha256,
            "sealed_results_sha256": dataset.results_sha256,
            "run_id": result.replay.run_id,
            "event_count": result.replay.event_count,
            "replay_dataset_hash": result.replay.dataset_hash,
            "settled_ticket_ids": list(result.settled_ticket_ids),
            "balance": str(result.balance),
            "paper_book_sha256": paper_book_sha256,
            "evaluation": {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(result.evaluation).items()},
            "portfolio": {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(result.portfolio).items()},
            "real_money_execution": False,
        }
        atomic_write_json(destination, payload)

    def close(self) -> None:
        self.book.save(self.book_path)
        self.store.close()
