from __future__ import annotations

import argparse
import json
import math
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent, MarketType
from autosport.replay import ReplayEngine
from autosport.storage import SQLiteMarketStore


@dataclass(frozen=True, slots=True)
class ReplayBenchmarkResult:
    event_count: int
    interval_ms: int
    source_duration_seconds: float
    engine_prepare_elapsed_seconds: float
    dispatch_elapsed_seconds: float
    measured_engine_total_elapsed_seconds: float
    dispatch_events_per_second: float
    measured_engine_total_events_per_second: float
    dispatch_realtime_multiplier: float
    measured_engine_total_realtime_multiplier: float
    accepted_events: int
    durable_history_events: int
    replay_dataset_hash: str
    mode: str = "fastest-event-driven"
    consumer_scope: str = "sqlite-market-store"
    fixture_construction_included: bool = False
    sqlite_store_open_included: bool = False
    provider_network_included: bool = False
    agent_callbacks_included: bool = False
    target_claim: bool = False


def _positive_int(name: str, value: object, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if minimum == 1 else f">= {minimum}"
        raise ValueError(f"{name} must be an integer {qualifier}")
    return value


def _positive_elapsed_seconds(start_ns: int, end_ns: int, field_name: str) -> float:
    if isinstance(start_ns, bool) or isinstance(end_ns, bool):
        raise ValueError(f"{field_name} clock samples must be integers")
    if not isinstance(start_ns, int) or not isinstance(end_ns, int):
        raise ValueError(f"{field_name} clock samples must be integers")
    elapsed_ns = end_ns - start_ns
    if elapsed_ns <= 0:
        raise ValueError(f"{field_name} measured clock did not advance")
    return elapsed_ns / 1_000_000_000


def _finite_positive_metric(name: str, value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"replay benchmark produced invalid {name}")
    return value


def _build_events(count: int, interval_ms: int) -> list[MarketEvent]:
    count = _positive_int("count", count, minimum=2)
    interval_ms = _positive_int("interval_ms", interval_ms)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events: list[MarketEvent] = []
    for index in range(count):
        observed = (base + timedelta(milliseconds=index * interval_ms)).isoformat()
        events.append(
            MarketEvent(
                event_id=f"replay-event-{index % 100}",
                market_id="winner",
                selection_id=f"selection-{index % 2}",
                decimal_odds=Decimal("1.80") + Decimal(index % 20) / Decimal("100"),
                observed_ts=observed,
                source_id="replay-benchmark",
                sequence=index + 1,
                market_type=MarketType.WINNER,
                source_ts=observed,
                ingest_ts=observed,
                metadata={"benchmark_index": index},
            )
        )
    return events


def _source_duration_seconds(events: list[MarketEvent]) -> float:
    if len(events) < 2:
        raise ValueError("at least two replay events are required")
    first = datetime.fromisoformat(events[0].observed_ts.replace("Z", "+00:00"))
    last = datetime.fromisoformat(events[-1].observed_ts.replace("Z", "+00:00"))
    duration = (last - first).total_seconds()
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("replay source duration must be positive and finite")
    return duration


def run_replay_benchmark(
    *,
    count: int = 5_000,
    interval_ms: int = 1_000,
) -> ReplayBenchmarkResult:
    """Measure fastest event-driven replay into durable local market state.

    Fixture construction and SQLite store opening are outside measurement. ReplayEngine
    preparation (ordering + dataset hash) and event dispatch are measured separately. The
    dispatch timer includes ReplayEngine.run(speed=0) plus synchronous
    SQLiteMarketStore.append callbacks. A second metric sums engine preparation + dispatch,
    so release evidence cannot silently present dispatch-only acceleration as total measured
    engine work. Provider/network acquisition and strategy/agent callbacks are deliberately
    excluded and surfaced as truth fields. This harness reports observations, not a V1 target
    claim.
    """

    count = _positive_int("count", count, minimum=2)
    interval_ms = _positive_int("interval_ms", interval_ms)
    events = _build_events(count, interval_ms)
    source_duration = _source_duration_seconds(events)

    prepare_started = time.perf_counter_ns()
    engine = ReplayEngine(events)
    prepare_ended = time.perf_counter_ns()
    prepare_elapsed = _positive_elapsed_seconds(
        prepare_started,
        prepare_ended,
        "engine_prepare_elapsed_seconds",
    )

    with tempfile.TemporaryDirectory(prefix="autosport-replay-benchmark-") as temp_dir:
        store = SQLiteMarketStore(Path(temp_dir) / "market.db")
        accepted = 0
        try:
            def consume(event: MarketEvent) -> None:
                nonlocal accepted
                if not store.append(event):
                    raise RuntimeError("replay benchmark event was not durably accepted")
                accepted += 1

            dispatch_started = time.perf_counter_ns()
            run = engine.run(consume, speed=0.0, run_id="replay-performance-benchmark")
            dispatch_ended = time.perf_counter_ns()
            dispatch_elapsed = _positive_elapsed_seconds(
                dispatch_started,
                dispatch_ended,
                "dispatch_elapsed_seconds",
            )
            durable_history_events = len(store.events())
        finally:
            store.close()

    if run.event_count != count:
        raise RuntimeError(
            f"replay benchmark dispatched {run.event_count} events; expected {count}"
        )
    if accepted != count:
        raise RuntimeError(
            f"replay benchmark durably accepted {accepted} events; expected {count}"
        )
    if durable_history_events != count:
        raise RuntimeError(
            "replay benchmark durable history count mismatch: "
            f"{durable_history_events} != {count}"
        )

    measured_total_elapsed = _finite_positive_metric(
        "measured engine total elapsed seconds",
        prepare_elapsed + dispatch_elapsed,
    )
    dispatch_throughput = _finite_positive_metric(
        "dispatch throughput",
        count / dispatch_elapsed,
    )
    measured_total_throughput = _finite_positive_metric(
        "measured engine total throughput",
        count / measured_total_elapsed,
    )
    dispatch_multiplier = _finite_positive_metric(
        "dispatch realtime multiplier",
        source_duration / dispatch_elapsed,
    )
    measured_total_multiplier = _finite_positive_metric(
        "measured engine total realtime multiplier",
        source_duration / measured_total_elapsed,
    )

    return ReplayBenchmarkResult(
        event_count=count,
        interval_ms=interval_ms,
        source_duration_seconds=source_duration,
        engine_prepare_elapsed_seconds=prepare_elapsed,
        dispatch_elapsed_seconds=dispatch_elapsed,
        measured_engine_total_elapsed_seconds=measured_total_elapsed,
        dispatch_events_per_second=dispatch_throughput,
        measured_engine_total_events_per_second=measured_total_throughput,
        dispatch_realtime_multiplier=dispatch_multiplier,
        measured_engine_total_realtime_multiplier=measured_total_multiplier,
        accepted_events=accepted,
        durable_history_events=durable_history_events,
        replay_dataset_hash=run.dataset_hash,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Autosport fastest event-driven replay throughput into durable local "
            "SQLite market state. Output is evidence only, not a target claim."
        )
    )
    parser.add_argument("--count", type=int, default=5_000)
    parser.add_argument("--interval-ms", type=int, default=1_000)
    args = parser.parse_args()
    result = run_replay_benchmark(count=args.count, interval_ms=args.interval_ms)
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
