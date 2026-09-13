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
from .endurance import EnduranceConfig, run_endurance
from .paper import PaperBook
from .parlayapi_provider import ParlayApiTableTennisProvider
from .portfolio import PortfolioEngine
from .recovery import reconcile_late_crashes
from .replay import ReplayEngine
from .research_strategy import ResearchStrategyPlan
from .run_registry import ReconciliationError
from .session import AutosportSession, ObservationResult
from .strategies import available_strategies


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
    dataset.add_argument(
        "--strategy",
        default="baseline-v1",
        choices=tuple(spec.strategy_id for spec in available_strategies()),
        help="canonical strategy implementation to execute and bind into experiment evidence",
    )
    dataset.add_argument(
        "--research-plan",
        type=Path,
        default=None,
        help="typed causal research-plan JSON required by research-replay-v1",
    )
    sub.add_parser("strategies", help="list canonical replay strategies and their runtime agents")
    verify = sub.add_parser("verify-dataset", help="verify sealed hashes and historical corpus governance without replay")
    verify.add_argument("path", type=Path)
    observe = sub.add_parser("observe-table-tennis", help="fetch one read-only table-tennis market snapshot")
    observe.add_argument("--workspace", type=Path, default=Path(".autosport-workspace"))
    observe.add_argument("--public-preview", action="store_true", help="use provider public preview without an API key")
    observe.add_argument("--max-items", type=int, default=250, help="hard maximum quotes requested from the provider")
    observe.add_argument("--show", type=int, default=50, help="maximum current quote lines to print")
    repair = sub.add_parser("repair-workspace", help="reconcile only late-crashed runs with durable hash-matched completion evidence")
    repair.add_argument("--workspace", type=Path, default=Path(".autosport-workspace"))
    endurance = sub.add_parser("endurance", help="run deterministic bounded ingestion/replay/restart/settlement stress checks")
    endurance.add_argument("--workspace", type=Path, default=Path(".autosport-endurance"))
    endurance.add_argument("--events", type=int, default=20_000)
    endurance.add_argument("--quote-keys", type=int, default=200)
    endurance.add_argument("--batch-size", type=int, default=500)
    endurance.add_argument("--restart-cycles", type=int, default=3)
    endurance.add_argument("--tickets", type=int, default=50)
    endurance.add_argument("--output", type=Path, default=None)
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


def run_dataset(
    path: Path,
    workspace: Path,
    bankroll: str,
    strategy_id: str = "baseline-v1",
    research_plan_path: Path | None = None,
) -> int:
    dataset = load_dataset(path)
    research_plan = (
        ResearchStrategyPlan.from_path(research_plan_path)
        if research_plan_path is not None
        else None
    )
    session = AutosportSession(
        workspace,
        bankroll,
        strategy_id=strategy_id,
        research_plan=research_plan,
    )
    try:
        result = session.run_dataset(dataset)
        print(f"run_id={result.replay.run_id}")
        print(f"strategy_id={session.strategy_id}")
        print(f"canonical_strategy_id={session.strategy.strategy_id}")
        if research_plan is not None:
            print(f"research_plan_sha256={research_plan.source_sha256}")
        print(f"events={result.replay.event_count}")
        print(f"balance={result.balance}")
        print(f"net_profit={result.evaluation.net_profit}")
        print(f"settled={len(result.settled_ticket_ids)}")
        if dataset.import_identity is not None:
            print(f"historical_import_identity={dataset.import_identity}")
    finally:
        session.close()
    return 0


def run_strategies() -> int:
    for spec in available_strategies():
        print(
            f"{spec.strategy_id} | agents={','.join(spec.agent_names)} | "
            f"opens_paper_tickets={str(spec.opens_paper_tickets).lower()} | "
            f"requires_research_plan={str(spec.requires_research_plan).lower()} | {spec.label}"
        )
    return 0


def run_verify_dataset(path: Path) -> int:
    dataset = load_dataset(path)
    print(
        f"dataset=OK schema_version={dataset.schema_version} "
        f"name={dataset.name} sport={dataset.sport}"
    )
    print(f"market_sha256={dataset.market_sha256}")
    print(f"sealed_results_sha256={dataset.results_sha256}")
    if dataset.governance is None:
        print("governance=LEGACY_FIXTURE_ONLY historical_proof=false")
        return 0
    governance = dataset.governance
    print(f"historical_import_identity={dataset.import_identity}")
    print(
        f"source_identity={governance.source_identity} "
        f"redistribution_policy={governance.redistribution_policy}"
    )
    print(
        f"coverage={governance.coverage_start_ts}..{governance.coverage_end_ts} "
        f"sources={','.join(governance.source_ids)} markets={','.join(governance.market_types)}"
    )
    print(
        f"acquired_at={governance.acquired_at} imported_at={governance.imported_at} "
        f"outcome_reveal_after={governance.outcome_reveal_after}"
    )
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


def run_endurance_command(
    workspace: Path,
    *,
    events: int,
    quote_keys: int,
    batch_size: int,
    restart_cycles: int,
    tickets: int,
    output: Path | None,
) -> int:
    config = EnduranceConfig(
        event_count=events,
        quote_keys=quote_keys,
        batch_size=batch_size,
        restart_cycles=restart_cycles,
        paper_tickets=tickets,
    )
    destination = output or (workspace / "endurance-report.json")
    report = run_endurance(workspace, config, output_path=destination)
    print(
        f"endurance={report.status} events={report.history_events} current={report.current_quotes} "
        f"duplicate_accepted={report.accepted_duplicate_pass} restarts={len(report.restart_hashes)} "
        f"reingest_hash_match={report.independent_reingest_hash_match}"
    )
    print(
        f"ingest_seconds={report.ingest_elapsed_seconds:.6f} "
        f"accepted_events_per_second={report.accepted_events_per_second:.2f} "
        f"peak_traced_memory_bytes={report.peak_traced_memory_bytes}"
    )
    print(f"invariant_fingerprint={report.stable_invariant_fingerprint}")
    print(f"report={destination}")
    for failure in report.failures:
        print(f"FAIL {failure}")
    return 0 if report.status == "PASS" else 5


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
        return run_dataset(
            args.path,
            args.workspace,
            args.bankroll,
            args.strategy,
            args.research_plan,
        )
    if args.command == "strategies":
        return run_strategies()
    if args.command == "verify-dataset":
        return run_verify_dataset(args.path)
    if args.command == "observe-table-tennis":
        return run_observe_table_tennis(
            args.workspace,
            public_preview=args.public_preview,
            max_items=args.max_items,
            show=args.show,
        )
    if args.command == "repair-workspace":
        return run_repair_workspace(args.workspace)
    if args.command == "endurance":
        return run_endurance_command(
            args.workspace,
            events=args.events,
            quote_keys=args.quote_keys,
            batch_size=args.batch_size,
            restart_cycles=args.restart_cycles,
            tickets=args.tickets,
            output=args.output,
        )
    if args.command == "gui":
        from .gui import main as gui_main
        return gui_main()
    raise AssertionError(args.command)
