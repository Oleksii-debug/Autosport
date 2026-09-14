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

from autosport.dataset import load_dataset
from autosport.domain import MarketEvent, MarketType
from autosport.replay import ReplayEngine
from autosport.storage import SQLiteMarketStore


@dataclass(frozen=True, slots=True)
class ReplayBenchmarkResult:
    event_count: int
    input_mode: str
    source_duration_seconds: float
    recording_span_is_synthetic: bool
    input_load_elapsed_seconds: float | None
    engine_prepare_elapsed_seconds: float
    dispatch_elapsed_seconds: float
    measured_engine_total_elapsed_seconds: float
    measured_input_pipeline_elapsed_seconds: float | None
    dispatch_events_per_second: float
    measured_engine_total_events_per_second: float
    measured_input_pipeline_events_per_second: float | None
    dispatch_realtime_multiplier: float
    measured_engine_total_realtime_multiplier: float
    measured_input_pipeline_realtime_multiplier: float | None
    accepted_events: int
    durable_history_events: int
    replay_dataset_hash: str
    dataset_name: str | None = None
    dataset_schema_version: int | None = None
    dataset_market_sha256: str | None = None
    dataset_import_identity: str | None = None
    release_evidence_input: bool = False
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
    instants: list[datetime] = []
    for event in events:
        try:
            instant = datetime.fromisoformat(event.observed_ts.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("replay observed_ts must be valid ISO-8601") from exc
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("replay observed_ts must be timezone-aware ISO-8601")
        instants.append(instant)
    duration = (max(instants) - min(instants)).total_seconds()
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("replay source duration must be positive and finite")
    return duration


def _measure_events(
    events: list[MarketEvent],
    *,
    input_mode: str,
    recording_span_is_synthetic: bool,
    input_load_elapsed_seconds: float | None = None,
    dataset_name: str | None = None,
    dataset_schema_version: int | None = None,
    dataset_market_sha256: str | None = None,
    dataset_import_identity: str | None = None,
    release_evidence_input: bool = False,
) -> ReplayBenchmarkResult:
    count = len(events)
    if count < 2:
        raise ValueError("at least two replay events are required")
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

    measured_engine_total = _finite_positive_metric(
        "measured engine total elapsed seconds",
        prepare_elapsed + dispatch_elapsed,
    )
    measured_input_pipeline: float | None = None
    if input_load_elapsed_seconds is not None:
        measured_input_pipeline = _finite_positive_metric(
            "measured input pipeline elapsed seconds",
            input_load_elapsed_seconds + measured_engine_total,
        )

    dispatch_throughput = _finite_positive_metric(
        "dispatch throughput",
        count / dispatch_elapsed,
    )
    measured_engine_total_throughput = _finite_positive_metric(
        "measured engine total throughput",
        count / measured_engine_total,
    )
    measured_input_pipeline_throughput = (
        None
        if measured_input_pipeline is None
        else _finite_positive_metric(
            "measured input pipeline throughput",
            count / measured_input_pipeline,
        )
    )
    dispatch_multiplier = _finite_positive_metric(
        "dispatch realtime multiplier",
        source_duration / dispatch_elapsed,
    )
    measured_engine_total_multiplier = _finite_positive_metric(
        "measured engine total realtime multiplier",
        source_duration / measured_engine_total,
    )
    measured_input_pipeline_multiplier = (
        None
        if measured_input_pipeline is None
        else _finite_positive_metric(
            "measured input pipeline realtime multiplier",
            source_duration / measured_input_pipeline,
        )
    )

    return ReplayBenchmarkResult(
        event_count=count,
        input_mode=input_mode,
        source_duration_seconds=source_duration,
        recording_span_is_synthetic=recording_span_is_synthetic,
        input_load_elapsed_seconds=input_load_elapsed_seconds,
        engine_prepare_elapsed_seconds=prepare_elapsed,
        dispatch_elapsed_seconds=dispatch_elapsed,
        measured_engine_total_elapsed_seconds=measured_engine_total,
        measured_input_pipeline_elapsed_seconds=measured_input_pipeline,
        dispatch_events_per_second=dispatch_throughput,
        measured_engine_total_events_per_second=measured_engine_total_throughput,
        measured_input_pipeline_events_per_second=measured_input_pipeline_throughput,
        dispatch_realtime_multiplier=dispatch_multiplier,
        measured_engine_total_realtime_multiplier=measured_engine_total_multiplier,
        measured_input_pipeline_realtime_multiplier=measured_input_pipeline_multiplier,
        accepted_events=accepted,
        durable_history_events=durable_history_events,
        replay_dataset_hash=run.dataset_hash,
        dataset_name=dataset_name,
        dataset_schema_version=dataset_schema_version,
        dataset_market_sha256=dataset_market_sha256,
        dataset_import_identity=dataset_import_identity,
        release_evidence_input=release_evidence_input,
    )


def run_replay_benchmark(
    *,
    count: int = 5_000,
    interval_ms: int = 1_000,
) -> ReplayBenchmarkResult:
    """Run a synthetic smoke/workload benchmark, never release-evidence input.

    Synthetic fixture construction is outside measurement. Because ``interval_ms`` defines an
    artificial recording span, the resulting realtime multiplier must not be presented as a
    typical-recording release claim. Engine preparation and dispatch remain useful for CI
    regression coverage and controlled machine-to-machine comparisons.
    """

    count = _positive_int("count", count, minimum=2)
    interval_ms = _positive_int("interval_ms", interval_ms)
    events = _build_events(count, interval_ms)
    return _measure_events(
        events,
        input_mode="synthetic",
        recording_span_is_synthetic=True,
        release_evidence_input=False,
    )


def run_replay_dataset_benchmark(dataset_root: str | Path) -> ReplayBenchmarkResult:
    """Measure a canonical SHA-verified dataset without inventing its recording span.

    Dataset manifest/governance verification and market-event loading are measured as a
    separate input phase through the production ``load_dataset`` boundary. Engine preparation
    and fastest event-driven dispatch remain separately observable. Schema-v2 governed input
    is marked as release-evidence-capable input, but ``target_claim`` remains false because the
    final target still depends on the exact integrated build, target Windows laptop, corpus
    representativeness, and published environment/result evidence.
    """

    load_started = time.perf_counter_ns()
    dataset = load_dataset(dataset_root)
    events = dataset.load_market_events()
    load_ended = time.perf_counter_ns()
    load_elapsed = _positive_elapsed_seconds(
        load_started,
        load_ended,
        "input_load_elapsed_seconds",
    )
    governed_release_input = dataset.schema_version == 2 and dataset.governance is not None
    return _measure_events(
        events,
        input_mode="canonical-dataset",
        recording_span_is_synthetic=False,
        input_load_elapsed_seconds=load_elapsed,
        dataset_name=dataset.name,
        dataset_schema_version=dataset.schema_version,
        dataset_market_sha256=dataset.market_sha256,
        dataset_import_identity=dataset.import_identity,
        release_evidence_input=governed_release_input,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Autosport fastest event-driven replay throughput into durable local "
            "SQLite market state. Output is evidence only, not a target claim."
        )
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        help=(
            "Canonical dataset directory. Uses production manifest/hash/governance loading "
            "and the dataset's real observed timestamp span."
        ),
    )
    parser.add_argument(
        "--count",
        type=int,
        default=5_000,
        help="Synthetic smoke event count; ignored when --dataset is supplied.",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=1_000,
        help="Synthetic smoke interval; ignored when --dataset is supplied.",
    )
    args = parser.parse_args()
    if args.dataset is not None:
        result = run_replay_dataset_benchmark(args.dataset)
    else:
        result = run_replay_benchmark(count=args.count, interval_ms=args.interval_ms)
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
