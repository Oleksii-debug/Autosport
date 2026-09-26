from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter_ns
from typing import Callable, Sequence

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorSnapshot


_AS_OF = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
_MAX_AGE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class DirtyReadBenchmarkResult:
    mirror_size: int
    dirty_key_count: int
    measured_iterations: int
    warmup_iterations: int
    verified_event_count: int
    full_view_p50_ms: float
    full_view_p95_ms: float
    full_view_p99_ms: float
    full_view_max_ms: float
    dirty_view_p50_ms: float
    dirty_view_p95_ms: float
    dirty_view_p99_ms: float
    dirty_view_max_ms: float
    p50_dirty_over_full_ratio: float


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


def _event(index: int) -> MarketEvent:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("index must be a non-negative integer")
    observed = (_AS_OF - timedelta(seconds=1)).isoformat()
    return MarketEvent(
        event_id=f"event-{index // 4}",
        market_id="winner",
        selection_id=f"selection-{index}",
        decimal_odds=Decimal("1.80"),
        observed_ts=observed,
        source_id=f"provider-{index % 4}",
        sequence=1,
        status="open",
        source_ts=observed,
        ingest_ts=observed,
        sport="table_tennis",
    )


def _build_fixture(
    mirror_size: int,
    dirty_key_count: int,
) -> tuple[MarketMirror, tuple[tuple[str, str], ...], frozenset[str]]:
    mirror_size = _positive_int("mirror_size", mirror_size)
    dirty_key_count = _positive_int("dirty_key_count", dirty_key_count)
    if dirty_key_count > mirror_size:
        raise ValueError("dirty_key_count must not exceed mirror_size")

    mirror = MarketMirror()
    keys: list[tuple[str, str]] = []
    selections: set[str] = set()
    stride = max(1, mirror_size // dirty_key_count)
    selected_indexes = {
        min(index * stride, mirror_size - 1)
        for index in range(dirty_key_count)
    }
    # Fill any collisions caused by integer division deterministically from the tail.
    candidate = mirror_size - 1
    while len(selected_indexes) < dirty_key_count:
        selected_indexes.add(candidate)
        candidate -= 1

    for index in range(mirror_size):
        event = _event(index)
        mirror.apply(event)
        if index in selected_indexes:
            keys.append((event.source_id, event.quote_key))
            selections.add(event.selection_id)

    keys.sort()
    if len(keys) != dirty_key_count or len(selections) != dirty_key_count:
        raise RuntimeError("benchmark fixture did not build the requested dirty-key set")
    return mirror, tuple(keys), frozenset(selections)


def _measure_call(call: Callable[[], MirrorSnapshot]) -> tuple[int, MirrorSnapshot]:
    started_ns = perf_counter_ns()
    result = call()
    elapsed_ns = perf_counter_ns() - started_ns
    if elapsed_ns <= 0:
        raise RuntimeError("latency clock did not advance for a measured read")
    return elapsed_ns, result


def _assert_equivalent(
    full_snapshot: MirrorSnapshot,
    dirty_snapshot: MirrorSnapshot,
    expected_count: int,
) -> None:
    if full_snapshot.revision != dirty_snapshot.revision:
        raise RuntimeError("full and dirty-key reads observed different mirror revisions")
    full_identity = tuple((event.source_id, event.quote_key) for event in full_snapshot.events)
    dirty_identity = tuple((event.source_id, event.quote_key) for event in dirty_snapshot.events)
    if full_identity != dirty_identity:
        raise RuntimeError("full and dirty-key reads returned different quote identities")
    if len(dirty_identity) != expected_count:
        raise RuntimeError(
            "dirty-key read did not return the requested event count: "
            f"expected={expected_count} actual={len(dirty_identity)}"
        )


def run_dirty_read_benchmark(
    *,
    mirror_size: int = 10_000,
    dirty_key_count: int = 32,
    iterations: int = 200,
    warmup: int = 20,
) -> DirtyReadBenchmarkResult:
    """Compare equivalent full-scan and bounded dirty-key Market Mirror reads.

    This harness measures local in-process read cost only. It does not include provider
    network time, decision-model work, portfolio recomputation or execution. Results
    are machine-specific evidence and are not a release/performance claim by themselves.
    """

    mirror_size = _positive_int("mirror_size", mirror_size)
    dirty_key_count = _positive_int("dirty_key_count", dirty_key_count)
    iterations = _positive_int("iterations", iterations)
    warmup = _nonnegative_int("warmup", warmup)
    if dirty_key_count > mirror_size:
        raise ValueError("dirty_key_count must not exceed mirror_size")

    mirror, keys, selections = _build_fixture(mirror_size, dirty_key_count)

    def full_read() -> MirrorSnapshot:
        return mirror.active_view(
            as_of=_AS_OF,
            max_age=_MAX_AGE,
            selection_ids=selections,
        )

    def dirty_read() -> MirrorSnapshot:
        return mirror.active_view_for_keys(
            keys,
            as_of=_AS_OF,
            max_age=_MAX_AGE,
        )

    for index in range(warmup):
        if index % 2 == 0:
            full_snapshot = full_read()
            dirty_snapshot = dirty_read()
        else:
            dirty_snapshot = dirty_read()
            full_snapshot = full_read()
        _assert_equivalent(full_snapshot, dirty_snapshot, dirty_key_count)

    full_samples_ns: list[int] = []
    dirty_samples_ns: list[int] = []
    for index in range(iterations):
        # Alternate measurement order to reduce systematic first/second-call bias.
        if index % 2 == 0:
            full_elapsed, full_snapshot = _measure_call(full_read)
            dirty_elapsed, dirty_snapshot = _measure_call(dirty_read)
        else:
            dirty_elapsed, dirty_snapshot = _measure_call(dirty_read)
            full_elapsed, full_snapshot = _measure_call(full_read)
        _assert_equivalent(full_snapshot, dirty_snapshot, dirty_key_count)
        full_samples_ns.append(full_elapsed)
        dirty_samples_ns.append(dirty_elapsed)

    full_p50_ms = _nearest_rank_percentile_ms(full_samples_ns, 50)
    full_p95_ms = _nearest_rank_percentile_ms(full_samples_ns, 95)
    full_p99_ms = _nearest_rank_percentile_ms(full_samples_ns, 99)
    full_max_ms = max(full_samples_ns) / 1_000_000.0
    dirty_p50_ms = _nearest_rank_percentile_ms(dirty_samples_ns, 50)
    dirty_p95_ms = _nearest_rank_percentile_ms(dirty_samples_ns, 95)
    dirty_p99_ms = _nearest_rank_percentile_ms(dirty_samples_ns, 99)
    dirty_max_ms = max(dirty_samples_ns) / 1_000_000.0

    for label, values in (
        ("full", (full_p50_ms, full_p95_ms, full_p99_ms, full_max_ms)),
        ("dirty", (dirty_p50_ms, dirty_p95_ms, dirty_p99_ms, dirty_max_ms)),
    ):
        if not (0 < values[0] <= values[1] <= values[2] <= values[3]):
            raise RuntimeError(f"{label} latency summary ordering is invalid")

    ratio = dirty_p50_ms / full_p50_ms
    if not math.isfinite(ratio) or ratio <= 0:
        raise RuntimeError("dirty/full p50 ratio is invalid")

    return DirtyReadBenchmarkResult(
        mirror_size=mirror_size,
        dirty_key_count=dirty_key_count,
        measured_iterations=iterations,
        warmup_iterations=warmup,
        verified_event_count=dirty_key_count,
        full_view_p50_ms=full_p50_ms,
        full_view_p95_ms=full_p95_ms,
        full_view_p99_ms=full_p99_ms,
        full_view_max_ms=full_max_ms,
        dirty_view_p50_ms=dirty_p50_ms,
        dirty_view_p95_ms=dirty_p95_ms,
        dirty_view_p99_ms=dirty_p99_ms,
        dirty_view_max_ms=dirty_max_ms,
        p50_dirty_over_full_ratio=ratio,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure equivalent full-scan vs bounded dirty-key Market Mirror read latency. "
            "Output is machine-specific evidence, not a release claim."
        )
    )
    parser.add_argument("--mirror-size", type=int, default=10_000)
    parser.add_argument("--dirty-keys", type=int, default=32)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = run_dirty_read_benchmark(
        mirror_size=args.mirror_size,
        dirty_key_count=args.dirty_keys,
        iterations=args.iterations,
        warmup=args.warmup,
    )
    print(
        "scope=market_mirror_equivalent_full_vs_dirty_key_reads "
        "provider_network_included=false portfolio_recompute_included=false "
        "execution_included=false target_claim=false "
        f"mirror_size={result.mirror_size} dirty_keys={result.dirty_key_count} "
        f"iterations={result.measured_iterations} warmup={result.warmup_iterations} "
        f"verified_events={result.verified_event_count} "
        f"full_p50_ms={result.full_view_p50_ms:.6f} "
        f"full_p95_ms={result.full_view_p95_ms:.6f} "
        f"full_p99_ms={result.full_view_p99_ms:.6f} "
        f"full_max_ms={result.full_view_max_ms:.6f} "
        f"dirty_p50_ms={result.dirty_view_p50_ms:.6f} "
        f"dirty_p95_ms={result.dirty_view_p95_ms:.6f} "
        f"dirty_p99_ms={result.dirty_view_p99_ms:.6f} "
        f"dirty_max_ms={result.dirty_view_max_ms:.6f} "
        f"p50_dirty_over_full_ratio={result.p50_dirty_over_full_ratio:.6f}"
    )


if __name__ == "__main__":
    main()
