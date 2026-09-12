from __future__ import annotations

import argparse
import uuid
from decimal import Decimal
from pathlib import Path

from .agents import AgentContext, AgentOrchestrator, MarketMirrorAgent, PaperBaselineAgent
from .domain import MarketEvent
from .paper import PaperBook
from .portfolio import PortfolioEngine
from .replay import ReplayEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autosport", description="Autosport paper/replay laboratory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="run built-in paper demonstration")
    replay = sub.add_parser("replay", help="run a JSONL market replay")
    replay.add_argument("path", type=Path)
    replay.add_argument("--bankroll", default="10000")
    sub.add_parser("gui", help="launch Windows-oriented GUI")
    return parser


def run_replay(path: Path, bankroll: str) -> int:
    book = PaperBook(Decimal(bankroll))
    run_id = str(uuid.uuid4())
    context = AgentContext(book, replay_run_id=run_id)
    orchestrator = AgentOrchestrator([MarketMirrorAgent(), PaperBaselineAgent()], context)
    engine = ReplayEngine.from_jsonl(path)
    run = engine.run(orchestrator.on_market_event, run_id=run_id)
    report = PortfolioEngine().analyse(list(book.tickets.values()))
    print(f"run_id={run.run_id}")
    print(f"dataset_hash={run.dataset_hash}")
    print(f"events={run.event_count}")
    print(f"virtual_balance={book.balance}")
    print(f"open_tickets={len(book.tickets)}")
    print(f"scenario_mode={report.mode} worst={report.worst_case} best={report.best_case}")
    return 0


def run_demo() -> int:
    events = [
        MarketEvent.from_dict({"event_id":"tt-001","market_id":"winner","selection_id":"alice","decimal_odds":"1.80","observed_ts":"2026-09-12T10:00:00+00:00","source_id":"demo","sequence":1,"market_type":"winner","metadata":{"paper_signal":True,"paper_signal_id":"demo-1"}}),
        MarketEvent.from_dict({"event_id":"tt-001","market_id":"winner","selection_id":"bob","decimal_odds":"2.10","observed_ts":"2026-09-12T10:00:01+00:00","source_id":"demo","sequence":2,"market_type":"winner"}),
        MarketEvent.from_dict({"event_id":"tt-001","market_id":"winner","selection_id":"alice","decimal_odds":"2.25","observed_ts":"2026-09-12T10:00:02+00:00","source_id":"demo","sequence":3,"market_type":"winner"}),
    ]
    book = PaperBook("10000")
    run_id = "demo-run"
    context = AgentContext(book, replay_run_id=run_id)
    orchestrator = AgentOrchestrator([MarketMirrorAgent(), PaperBaselineAgent("50")], context)
    run = ReplayEngine(events).run(orchestrator.on_market_event, run_id=run_id)
    print(f"run={run.run_id} dataset={run.dataset_hash[:12]} events={context.event_count} balance={book.balance} tickets={len(book.tickets)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "demo":
        return run_demo()
    if args.command == "replay":
        return run_replay(args.path, args.bankroll)
    if args.command == "gui":
        from .gui import main as gui_main
        return gui_main()
    raise AssertionError(args.command)
