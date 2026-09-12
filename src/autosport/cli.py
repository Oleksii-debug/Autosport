from __future__ import annotations

import argparse
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .agents import AgentContext, AgentOrchestrator, MarketMirrorAgent, PaperBaselineAgent
from .dataset import load_dataset
from .domain import MarketEvent
from .paper import PaperBook
from .parlayapi_provider import ParlayApiTableTennisProvider
from .portfolio import PortfolioEngine
from .recovery import reconcile_late_crashes
from .replay import ReplayEngine
from .run_registry import ReconciliationError
from .session import AutosportSession, ObservationResult


ProviderFactory = Callable[..., ParlayApiTableTennisProvider]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autosport", description="Autosport paper/replay laboratory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="run built-in paper demonstration")
    replay = sub.add_parser("replay", help="run a raw JSONL market replay")
    replay.add_argument("path", type=Path)
    replay.add_argument("--bankroll", default="10000")
    dataset = sub.add_parser("dataset", help="run a sealed dataset package end to end")
    dataset.add_argument("path", type=Path)
    dataset.add_argument("--workspace", type=Path, default=Path(".autosport-workspace"))
    dataset.add_argument("--bankroll", default="10000")
    observe = sub.add_parser("observe-table-tennis", help="fetch one read-only table-tennis market snapshot")
    observe.add_argument("--workspace", type=Path, default=Path(".autosport-workspace"))
    observe.add_argument("--public-preview", action="store_true", help="use provider public preview without an API key")
    observe.add_argument("--max-items", type=int, default=250, help="hard maximum quotes requested from the provider")
    observe.add_argument("--show", type=int, default=50, help="maximum current quote lines to print")
    repair = sub.add_parser("repair-workspace", help="reconcile only late-crashed runs with durable hash-matched completion evidence")
    repair.add_argument("--workspace", type=Path, default=Path(".autosport-workspace"))
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
    print(f"run_id={run.run_id}\ndataset_hash={run.dataset_hash}\nevents={run.event_count}\nvirtual_balance={book.balance}")
    print(f"scenario_mode={report.mode} worst={report.worst_case} best={report.best_case}")
    return 0


def run_dataset(path: Path, workspace: Path, bankroll: str) -> int:
    dataset = load_dataset(path)
    session = AutosportSession(workspace, bankroll)
    try:
        result = session.run_dataset(dataset)
        print(f"run_id={result.replay.run_id}")
        print(f"events={result.replay.event_count}")
        print(f"balance={result.balance}")
        print(f"net_profit={result.evaluation.net_profit}")
        print(f"settled={len(result.settled_ticket_ids)}")
    finally:
        session.close()
    return 0


def run_observe_table_tennis(
    workspace: Path,
    *,
    public_preview: bool,
    max_items: int,
    show: int,
    provider_factory: ProviderFactory = ParlayApiTableTennisProvider,
) -> int:
    if show < 0:
        raise ValueError("show must be non-negative")
    api_key = None if public_preview else os.environ.get("AUTOSPORT_PARLAYAPI_KEY")
    if not public_preview and not api_key:
        print(
            "AUTOSPORT_PARLAYAPI_KEY is not set. Set it in the environment or use --public-preview.",
        )
        return 2
    provider = provider_factory(api_key, public_preview=public_preview)
    session = AutosportSession(workspace)
    try:
        result = session.observe_provider_once(provider, max_items=max_items)
        _print_observation(result, show)
    finally:
        session.close()
    return 0


def run_repair_workspace(workspace: Path) -> int:
    try:
        report = reconcile_late_crashes(workspace)
    except ReconciliationError as exc:
        print(f"repair=FAIL_CLOSED error={exc}")
        return 3
    print(
        f"repair=OK reconciled={len(report.reconciled_keys)} "
        f"unresolved_without_summary={len(report.unresolved_without_summary)}"
    )
    for key in report.reconciled_keys:
        print(f"RECONCILED {key}")
    for key in report.unresolved_without_summary:
        print(f"UNRESOLVED_NO_DURABLE_SUMMARY {key}")
    return 4 if report.unresolved_without_summary else 0


def _print_observation(result: ObservationResult, show: int) -> None:
    flags = ",".join(result.stats.quality_flags) if result.stats.quality_flags else "none"
    print(
        f"source={result.stats.source_id} health={result.health.status} "
        f"received={result.stats.received} accepted={result.stats.accepted} "
        f"rejected={result.stats.rejected} flags={flags}"
    )
    print(
        f"current_quotes={len(result.current_quotes)} "
        f"latest_source_ts={result.health.latest_source_ts or '-'} cursor={result.health.last_cursor or '-'}"
    )
    for event in result.current_quotes[:show]:
        print(
            f"{event.event_id} | {event.market_type.value} | {event.market_id} | "
            f"{event.selection_id} | odds={event.decimal_odds} | source_ts={event.source_ts or '-'}"
        )
    if len(result.current_quotes) > show:
        print(f"... {len(result.current_quotes) - show} more current quotes not printed")


def run_demo() -> int:
    events = [
        MarketEvent.from_dict({"event_id":"tt-001","market_id":"winner","selection_id":"alice","decimal_odds":"1.80","observed_ts":"2026-09-12T10:00:00+00:00","source_id":"demo","sequence":1,"market_type":"winner","metadata":{"paper_signal":True,"paper_signal_id":"demo-1"}}),
        MarketEvent.from_dict({"event_id":"tt-001","market_id":"winner","selection_id":"bob","decimal_odds":"2.10","observed_ts":"2026-09-12T10:00:01+00:00","source_id":"demo","sequence":2,"market_type":"winner"}),
        MarketEvent.from_dict({"event_id":"tt-001","market_id":"winner","selection_id":"alice","decimal_odds":"2.25","observed_ts":"2026-09-12T10:00:02+00:00","source_id":"demo","sequence":3,"market_type":"winner"}),
    ]
    book = PaperBook("10000")
    context = AgentContext(book, replay_run_id="demo-run")
    orchestrator = AgentOrchestrator([MarketMirrorAgent(), PaperBaselineAgent("50")], context)
    run = ReplayEngine(events).run(orchestrator.on_market_event, run_id="demo-run")
    print(f"run={run.run_id} dataset={run.dataset_hash[:12]} events={context.event_count} balance={book.balance} tickets={len(book.tickets)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "demo":
        return run_demo()
    if args.command == "replay":
        return run_replay(args.path, args.bankroll)
    if args.command == "dataset":
        return run_dataset(args.path, args.workspace, args.bankroll)
    if args.command == "observe-table-tennis":
        return run_observe_table_tennis(
            args.workspace,
            public_preview=args.public_preview,
            max_items=args.max_items,
            show=args.show,
        )
    if args.command == "repair-workspace":
        return run_repair_workspace(args.workspace)
    if args.command == "gui":
        from .gui import main as gui_main
        return gui_main()
    raise AssertionError(args.command)
