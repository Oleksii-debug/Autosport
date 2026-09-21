from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter_ns
from typing import Callable, Sequence

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import (
    FocusedMirrorDependencyIndex,
    MirrorInvalidationBatch,
)


ClockNs = Callable[[], int]


@dataclass(frozen=True, slots=True)
class MirrorDependencyRoutingLatencyResult:
    input_count: int
    measured_updates: int
    warmup_updates: int
    affected_per_update: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _nearest_rank_percentile_ms(samples_ns: Sequence[int], percentile: float) -> float:
    if not samples_ns:
        raise ValueError("latency samples must not be empty")
    if isinstance(percentile, bool) or not isinstance(percentile, (int, float)):
        raise ValueError("percentile must be a finite number in (0, 100]")
    percentile_value = float(percentile)
    if not math.isfinite(percentile_value) or percentile_value <= 0 or percentile_value > 100:
        raise ValueError("percentile must be a finite number in (0, 100]")

    normalized: list[int] = []
    for sample in samples_ns:
        if isinstance(sample, bool) or not isinstance(sample, int) or sample <= 0:
            raise ValueError("latency samples must be positive integer nanoseconds")
        normalized.append(sample)

    ordered = sorted(normalized)
    rank = math.ceil((percentile_value / 100.0) * len(ordered))
    return ordered[rank - 1] / 1_000_000.0


def _selection(index: int) -> str:
    return f"selection-{index:06d}"


def _input_id(index: int) -> str:
    return f"decision-{index:06d}"


def _event(*, key_index: int, sequence: int) -> MarketEvent:
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamp = (origin + timedelta(microseconds=sequence)).isoformat()
    return MarketEvent(
        event_id="event-1",
        market_id="winner",
        selection_id=_selection(key_index),
        decimal_odds=Decimal("2.00"),
        observed_ts=timestamp,
        source_id="benchmark-provider",
        sequence=sequence,
        status="open",
        source_ts=timestamp,
        ingest_ts=timestamp,
        sport="table_tennis",
    )


def run_latency_benchmark(
    *,
    input_count: int = 1_000,
    measured_updates: int = 5_000,
    warmup_updates: int = 200,
    clock_ns: ClockNs = perf_counter_ns,
) -> MirrorDependencyRoutingLatencyResult:
    """Measure dirty quote -> affected decision-input routing only.

    Mirror mutation, persistence, provider/network work, intent generation, portfolio
    recomputation, and execution are deliberately outside the timer. This isolates the
    Stage-F dependency-routing boundary and produces machine-specific measurement
    evidence only; it does not establish a release threshold or target-machine pass.
    """

    input_count = _positive_int("input_count", input_count)
    measured_updates = _positive_int("measured_updates", measured_updates)
    warmup_updates = _nonnegative_int("warmup_updates", warmup_updates)
    if not callable(clock_ns):
        raise TypeError("clock_ns must be callable")

    mirror = MarketMirror()
    dependencies = FocusedMirrorDependencyIndex(mirror)
    for index in range(input_count):
        dependencies.register(
            _input_id(index),
            source_ids="benchmark-provider",
            event_ids="event-1",
            market_ids="winner",
            selection_ids=_selection(index),
            sports="table_tennis",
        )

    samples_ns: list[int] = []
    total = warmup_updates + measured_updates
    for update_index in range(total):
        key_index = update_index % input_count
        event = _event(key_index=key_index, sequence=update_index + 1)
        apply_result = mirror.apply(event)
        batch = MirrorInvalidationBatch(
            changed_keys=((apply_result.source_id, apply_result.quote_key),),
            full_refresh_required=False,
            has_more=False,
        )
        expected = (_input_id(key_index),)

        if update_index < warmup_updates:
            affected = dependencies.affected_inputs(batch)
            if affected != expected:
                raise RuntimeError(
                    "warmup dependency routing returned wrong input identities: "
                    f"expected={expected!r} actual={affected!r}"
                )
            continue

        started_ns = clock_ns()
        affected = dependencies.affected_inputs(batch)
        finished_ns = clock_ns()
        if affected != expected:
            raise RuntimeError(
                "measured dependency routing returned wrong input identities: "
                f"expected={expected!r} actual={affected!r}"
            )
        elapsed_ns = finished_ns - started_ns
        if elapsed_ns <= 0:
            raise RuntimeError("latency clock did not advance for a measured update")
        samples_ns.append(elapsed_ns)

    if len(samples_ns) != measured_updates:
        raise RuntimeError(
            "dependency-routing workload did not complete exactly: "
            f"requested={measured_updates} samples={len(samples_ns)}"
        )

    p50_ms = _nearest_rank_percentile_ms(samples_ns, 50)
    p95_ms = _nearest_rank_percentile_ms(samples_ns, 95)
    p99_ms = _nearest_rank_percentile_ms(samples_ns, 99)
    max_ms = max(samples_ns) / 1_000_000.0
    if not (0 < p50_ms <= p95_ms <= p99_ms <= max_ms):
        raise RuntimeError("latency summary ordering is invalid")

    return MirrorDependencyRoutingLatencyResult(
        input_count=input_count,
        measured_updates=measured_updates,
        warmup_updates=warmup_updates,
        affected_per_update=1,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        p99_ms=p99_ms,
        max_ms=max_ms,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Autosport Market Mirror dirty-key dependency routing latency. "
            "The result is machine-specific evidence, not a release claim."
        )
    )
    parser.add_argument("--inputs", type=int, default=1_000, help="registered focused decision inputs")
    parser.add_argument("--updates", type=int, default=5_000, help="measured dirty-key updates")
    parser.add_argument("--warmup", type=int, default=200, help="unmeasured warmup updates")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = run_latency_benchmark(
        input_count=args.inputs,
        measured_updates=args.updates,
        warmup_updates=args.warmup,
    )
    print(
        "scope=market_mirror_dirty_key_to_affected_input_routing "
        "provider_network_included=false persistence_included=false "
        "mirror_apply_included=false portfolio_recompute_included=false "
        "target_machine_required=true target_claim=false "
        f"inputs={result.input_count} measured={result.measured_updates} "
        f"warmup={result.warmup_updates} affected_per_update={result.affected_per_update} "
        f"p50_ms={result.p50_ms:.6f} p95_ms={result.p95_ms:.6f} "
        f"p99_ms={result.p99_ms:.6f} max_ms={result.max_ms:.6f}"
    )


if __name__ == "__main__":
    main()
