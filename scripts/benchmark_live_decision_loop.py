from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from autosport.decision_ledger import EconomicDecisionAuthority
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
from autosport.live_decision_loop import (
    LiveCycleResult,
    LiveLoopBounds,
    LiveDecisionMode,
    PersistentLiveDecisionLoop,
)
from autosport.market_bus import MarketEventBus
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from autosport.scientific_registry import ScientificRegistry, StrategyVersion
from autosport.storage import SQLiteMarketStore


_SCHEMA = "autosport.live_decision_loop_benchmark"
_SCHEMA_VERSION = 1
_START = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)
_STRATEGY_VERSION_ID = "benchmark-live-loop-strategy-v1"
_SOURCE_SHA256 = hashlib.sha256(
    b"scripts.benchmark_live_decision_loop:_EmptyIntentFactory:v1"
).hexdigest()
_ENVIRONMENT_SHA256 = hashlib.sha256(
    b"scripts.benchmark_live_decision_loop:environment:v1"
).hexdigest()
_CONFIG_SHA256 = hashlib.sha256(
    b"scripts.benchmark_live_decision_loop:empty-intent-config:v1"
).hexdigest()


class _ManualClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _DurableObserver:
    """Deterministic offline observer used to define the benchmark boundary.

    Non-empty batches include durable SQLite insertion and persist-first bus delivery.
    Empty batches are an O(1) provider-no-change stand-in. No network or sleep is used.
    """

    def __init__(
        self,
        workspace: Path,
        batches: list[tuple[MarketEvent, ...]],
    ) -> None:
        self.workspace = workspace
        self.batches = list(batches)

    def __call__(self, updates) -> object:
        batch = self.batches.pop(0) if self.batches else ()
        if not batch:
            return object()
        store = SQLiteMarketStore(self.workspace / "market.db")
        try:
            bus = MarketEventBus(store)
            bus.subscribe(updates.accept_persisted)
            bus.publish_many(batch)
        finally:
            store.close()
        return object()


class _EmptyIntentFactory:
    strategy_version_id = _STRATEGY_VERSION_ID

    def __call__(self, input_id, snapshot):
        del input_id, snapshot
        return ()


def _event(*, selection: str, sequence: int, odds: str = "2.00") -> MarketEvent:
    timestamp = _START.isoformat()
    return MarketEvent(
        event_id="benchmark-event",
        market_id="benchmark-market",
        selection_id=selection,
        decimal_odds=Decimal(odds),
        observed_ts=timestamp,
        source_id="benchmark-provider",
        sequence=sequence,
        status="open",
        source_ts=timestamp,
        ingest_ts=timestamp,
    )


def _authority() -> EconomicDecisionAuthority:
    goal = EconomicGoalContract(
        goal_id="benchmark-goal",
        revision=1,
        bankroll_id="benchmark-bankroll",
        currency="EUR",
    )
    return EconomicDecisionAuthority(
        goal,
        PaperRiskPolicy(economic_goal=goal),
    )


def _registry(workspace: Path) -> ScientificRegistry:
    registry = ScientificRegistry.initialize_pristine(
        workspace / "scientific_registry.json"
    )
    registry.append(
        StrategyVersion(
            strategy_version_id=_STRATEGY_VERSION_ID,
            canonical_strategy_id="benchmark-live-loop-strategy",
            source_sha256=_SOURCE_SHA256,
            environment_sha256=_ENVIRONMENT_SHA256,
            config_sha256=_CONFIG_SHA256,
            created_at=_START.isoformat(),
        )
    )
    return registry


def _loop(
    workspace: Path,
    *,
    batches: list[tuple[MarketEvent, ...]],
    bounds: LiveLoopBounds | None = None,
) -> PersistentLiveDecisionLoop:
    loop = PersistentLiveDecisionLoop(
        workspace,
        loop_id="benchmark-live-loop",
        mode=LiveDecisionMode.PAPER,
        book=PaperBook("1000"),
        authority=_authority(),
        intent_factory=_EmptyIntentFactory(),
        scientific_registry=_registry(workspace),
        observation_runner=_DurableObserver(workspace, batches),
        bounds=bounds,
        max_quote_age=timedelta(seconds=5),
        clock=_ManualClock(_START),
    )
    loop.register_input("all-markets")
    return loop


def _nearest_rank(values: list[int], percentile: float) -> int:
    if not values:
        raise ValueError("at least one timing sample is required")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _summary(
    durations_ns: list[int],
    results: list[LiveCycleResult],
) -> dict[str, object]:
    if len(durations_ns) != len(results) or not durations_ns:
        raise ValueError("timings and results must be non-empty and aligned")
    if any(type(value) is not int or value < 0 for value in durations_ns):
        raise ValueError("timing samples must be non-negative integer nanoseconds")
    ordered = sorted(durations_ns)
    count = len(ordered)
    median_ns = (
        ordered[count // 2]
        if count % 2
        else (ordered[(count // 2) - 1] + ordered[count // 2]) // 2
    )
    statuses = Counter(result.status.value for result in results)
    return {
        "sample_count": count,
        "status_counts": dict(sorted(statuses.items())),
        "min_ns": ordered[0],
        "median_ns": median_ns,
        "p95_ns": _nearest_rank(ordered, 0.95),
        "max_ns": ordered[-1],
        "samples_ns": durations_ns,
    }


def _measure(
    loop: PersistentLiveDecisionLoop,
    *,
    warmup_cycles: int,
    measured_cycles: int,
    timer_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, object]:
    for _ in range(warmup_cycles):
        loop.run_cycle()
    durations: list[int] = []
    results: list[LiveCycleResult] = []
    for _ in range(measured_cycles):
        started = timer_ns()
        result = loop.run_cycle()
        finished = timer_ns()
        elapsed = finished - started
        if elapsed < 0:
            raise RuntimeError("monotonic benchmark timer moved backwards")
        durations.append(elapsed)
        results.append(result)
    return _summary(durations, results)


def _scenario_no_change(
    workspace: Path,
    *,
    warmup_cycles: int,
    measured_cycles: int,
) -> dict[str, object]:
    batches = [((_event(selection="stable", sequence=1)),)]
    batches.extend(() for _ in range(warmup_cycles + measured_cycles + 1))
    loop = _loop(workspace, batches=batches)
    try:
        # Prime durable state and clear registration/full-refresh work outside timing.
        loop.run_cycle()
        return _measure(
            loop,
            warmup_cycles=warmup_cycles,
            measured_cycles=measured_cycles,
        )
    finally:
        loop.close()


def _scenario_single_dirty(
    workspace: Path,
    *,
    warmup_cycles: int,
    measured_cycles: int,
) -> dict[str, object]:
    total = warmup_cycles + measured_cycles
    batches: list[tuple[MarketEvent, ...]] = [
        (_event(selection="dirty", sequence=1, odds="2.00"),)
    ]
    for index in range(total):
        sequence = index + 2
        odds = "2.01" if sequence % 2 else "2.02"
        batches.append((_event(selection="dirty", sequence=sequence, odds=odds),))
    loop = _loop(workspace, batches=batches)
    try:
        # Prime so measured cycles represent incremental single-key invalidation.
        loop.run_cycle()
        return _measure(
            loop,
            warmup_cycles=warmup_cycles,
            measured_cycles=measured_cycles,
        )
    finally:
        loop.close()


def _scenario_backpressure(
    workspace: Path,
    *,
    warmup_cycles: int,
    measured_cycles: int,
    burst_size: int,
) -> dict[str, object]:
    if burst_size < 2:
        raise ValueError("burst_size must be at least 2")
    total = warmup_cycles + measured_cycles
    batches: list[tuple[MarketEvent, ...]] = []
    for cycle in range(total):
        batches.append(
            tuple(
                _event(
                    selection=f"burst-{cycle}-{offset}",
                    sequence=1,
                    odds="2.00",
                )
                for offset in range(burst_size)
            )
        )
    max_dirty_keys = max(4096, total * burst_size * 2)
    loop = _loop(
        workspace,
        batches=batches,
        bounds=LiveLoopBounds(
            observation_max_items=max(250, burst_size),
            max_dirty_keys=max_dirty_keys,
            max_dirty_per_cycle=1,
            max_registered_inputs=64,
        ),
    )
    try:
        return _measure(
            loop,
            warmup_cycles=warmup_cycles,
            measured_cycles=measured_cycles,
        )
    finally:
        loop.close()


def run_benchmark(
    work_root: Path,
    *,
    source_sha: str,
    warmup_cycles: int = 20,
    measured_cycles: int = 200,
    burst_size: int = 4,
) -> dict[str, object]:
    if type(warmup_cycles) is not int or warmup_cycles < 0:
        raise ValueError("warmup_cycles must be a non-negative integer")
    if type(measured_cycles) is not int or measured_cycles <= 0:
        raise ValueError("measured_cycles must be a positive integer")
    if type(burst_size) is not int or burst_size < 2:
        raise ValueError("burst_size must be an integer >= 2")
    if (
        type(source_sha) is not str
        or len(source_sha) != 40
        or any(character not in "0123456789abcdef" for character in source_sha)
    ):
        raise ValueError("source_sha must be a lowercase 40-character Git commit SHA")

    work_root.mkdir(parents=True, exist_ok=True)
    timer = time.get_clock_info("perf_counter")
    report: dict[str, object] = {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "source_sha": source_sha,
        "measurement_boundary": (
            "PersistentLiveDecisionLoop.run_cycle including the deterministic offline "
            "observation callback; dirty scenarios include durable SQLite/event-bus "
            "publication, while no-change observation is O(1). Network is excluded."
        ),
        "threshold_policy": "measurement_only_no_pass_fail_threshold",
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "perf_counter_resolution_seconds": timer.resolution,
            "perf_counter_monotonic": timer.monotonic,
        },
        "workload": {
            "warmup_cycles": warmup_cycles,
            "measured_cycles": measured_cycles,
            "backpressure_burst_size": burst_size,
            "backpressure_max_dirty_per_cycle": 1,
        },
        "scenarios": {},
    }
    scenarios = report["scenarios"]
    assert isinstance(scenarios, dict)
    scenarios["no_change"] = _scenario_no_change(
        work_root / "no-change",
        warmup_cycles=warmup_cycles,
        measured_cycles=measured_cycles,
    )
    scenarios["single_dirty"] = _scenario_single_dirty(
        work_root / "single-dirty",
        warmup_cycles=warmup_cycles,
        measured_cycles=measured_cycles,
    )
    scenarios["backpressure"] = _scenario_backpressure(
        work_root / "backpressure",
        warmup_cycles=warmup_cycles,
        measured_cycles=measured_cycles,
        burst_size=burst_size,
    )
    return report


def _clean_source_sha(repo_root: Path) -> str:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("benchmark requires a readable Git checkout") from exc
    if status.stdout:
        raise RuntimeError(
            "benchmark evidence requires a clean checkout; commit or discard changes first"
        )
    if (
        len(head) != 40
        or head != head.lower()
        or any(character not in "0123456789abcdef" for character in head)
    ):
        raise RuntimeError("git rev-parse HEAD did not return a canonical commit SHA")
    return head


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure bounded PersistentLiveDecisionLoop cycle latency without setting "
            "a performance pass/fail threshold."
        )
    )
    parser.add_argument("--warmup-cycles", type=int, default=20)
    parser.add_argument("--cycles", type=int, default=200)
    parser.add_argument("--burst-size", type=int, default=4)
    parser.add_argument(
        "--json-out",
        type=Path,
        help="optional path for canonical JSON evidence; stdout is always emitted",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    source_sha = _clean_source_sha(repo_root)
    with tempfile.TemporaryDirectory(prefix="autosport-live-loop-benchmark-") as directory:
        report = run_benchmark(
            Path(directory),
            source_sha=source_sha,
            warmup_cycles=args.warmup_cycles,
            measured_cycles=args.cycles,
            burst_size=args.burst_size,
        )
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded + "\n", encoding="utf-8")
    sys.stdout.write(encoded + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
