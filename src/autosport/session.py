from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from .agents import AgentContext, AgentOrchestrator, MarketMirrorAgent, PaperBaselineAgent
from .dataset import ReplayDataset
from .decision_ledger import JsonlDecisionLedger
from .evaluation import EvaluationSummary, evaluate
from .paper import PaperBook
from .portfolio import PortfolioEngine, PortfolioReport
from .replay import ReplayEngine, ReplayRun
from .settlement import SettlementEngine
from .storage import SQLiteMarketStore


@dataclass(frozen=True, slots=True)
class SessionResult:
    replay: ReplayRun
    settled_ticket_ids: tuple[str, ...]
    balance: Decimal
    evaluation: EvaluationSummary
    portfolio: PortfolioReport


class AutosportSession:
    """One end-to-end V1 runtime: replay -> agents -> paper book -> settlement -> evaluation -> recovery."""

    def __init__(self, workspace: str | Path, initial_bankroll: Decimal | str = "10000") -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteMarketStore(self.workspace / "market.db")
        self.book_path = self.workspace / "paper_book.json"
        self.book = PaperBook.load(self.book_path) if self.book_path.exists() else PaperBook(initial_bankroll)
        self.ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
        self.portfolio_engine = PortfolioEngine()

    def _runtime(self, run_id: str) -> AgentOrchestrator:
        context = AgentContext(self.book, replay_run_id=run_id, decision_ledger=self.ledger)
        return AgentOrchestrator([MarketMirrorAgent(), PaperBaselineAgent("50")], context)

    def run_dataset(self, dataset: ReplayDataset, speed: float = 0.0) -> SessionResult:
        run_id = str(uuid.uuid4())
        orchestrator = self._runtime(run_id)
        engine = ReplayEngine(dataset.load_market_events())

        def consume(event) -> None:
            self.store.append(event)
            orchestrator.on_market_event(event)

        replay = engine.run(consume, speed=speed, run_id=run_id)
        # Sealed result bytes are opened only after strategy event delivery is complete.
        settlement = SettlementEngine()
        settlement.record(dataset.load_results_after_replay())
        settled = tuple(settlement.settle_ready(self.book))
        self.book.save(self.book_path)
        evaluation = evaluate(self.book)
        portfolio = self.portfolio_engine.analyse(list(self.book.tickets.values()))
        result = SessionResult(replay, settled, self.book.balance, evaluation, portfolio)
        self._write_run_summary(dataset, result)
        return result

    def _write_run_summary(self, dataset: ReplayDataset, result: SessionResult) -> None:
        payload = {
            "dataset_name": dataset.name,
            "sport": dataset.sport,
            "market_sha256": dataset.market_sha256,
            "sealed_results_sha256": dataset.results_sha256,
            "run_id": result.replay.run_id,
            "event_count": result.replay.event_count,
            "replay_dataset_hash": result.replay.dataset_hash,
            "settled_ticket_ids": list(result.settled_ticket_ids),
            "balance": str(result.balance),
            "evaluation": {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(result.evaluation).items()},
            "portfolio": {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(result.portfolio).items()},
            "real_money_execution": False,
        }
        destination = self.workspace / f"run-{result.replay.run_id}.json"
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def close(self) -> None:
        self.book.save(self.book_path)
        self.store.close()
