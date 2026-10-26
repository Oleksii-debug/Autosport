from __future__ import annotations

import argparse
import json
import math
import platform
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter_ns
from typing import Callable, Sequence

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
)

Clock = Callable[[], int]
_ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class LiveInvalidationRoutingResult:
    quote_count: int
    dependency_count: int
    changed_count: int
    measured_samples: int
    warmup_samples: int
    expected_affected_count: int
    dependency_change_pairs: int
    route_samples_ns: tuple[int, ...]
    projection_samples_ns: tuple[int, ...]
    route_p50_ms: float
    route_p95_ms: float
    route_p99_ms: float
    route_max_ms: float
    projection_p50_ms: float
    projection_p95_ms: float
    projection_p99_ms: float
    projection_max_ms: float


@dataclass(frozen=True, slots=True)
class _Fixture:
    buffer: BoundedMirrorInvalidationBuffer
    dependencies: FocusedMirrorDependencyIndex
    expected_affected_ids: tuple[str, ...]


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
    if not math.isfinite(percentile_value) or not 0 < percentile_value <= 100:
        raise ValueError("percentile must be a finite number in (0, 100]")
    normalized: list[int] = []
    for sample in samples_ns:
        if isinstance(sample, bool) or not isinstance(sample, int) or sample <= 0:
            raise ValueError("latency samples must be positive integer nanoseconds")
        normalized.append(sample)
    ordered = sorted(normalized)
    rank = math.ceil((percentile_value / 100.0) * len(ordered))
    return ordered[rank - 1] / 1_000_000.0


def _event(*, quote_index: int, sequence: int) -> MarketEvent:
    quote_index = _nonnegative_int("quote_index", quote_index)
    sequence = _positive_int("sequence", sequence)
    observed = (_ORIGIN + timedelta(milliseconds=sequence)).isoformat()
    return MarketEvent(
        event_id=f"event-{quote_index}",
        market_id="winner",
        selection_id=f"selection-{quote_index}",
        decimal_odds=Decimal("2.00") + (Decimal(sequence) / Decimal("10000")),
        observed_ts=observed,
        source_id="benchmark",
        sequence=sequence,
        status="open",
        source_ts=observed,
        ingest_ts=observed,
    )


def _build_fixture(
    *,
    quote_count: int,
    dependency_count: int,
    changed_count: int,
) -> _Fixture:
    quote_count = _positive_int("quote_count", quote_count)
    dependency_count = _positive_int("dependency_count", dependency_count)
    changed_count = _positive_int("changed_count", changed_count)
    if changed_count > quote_count:
        raise ValueError("changed_count must not exceed quote_count")

    mirror = MarketMirror()
    buffer = BoundedMirrorInvalidationBuffer(
        mirror,
        max_dirty_keys=quote_count + 1,
    )
    for quote_index in range(quote_count):
        buffer.accept_persisted(_event(quote_index=quote_index, sequence=1))
    priming = buffer.drain(max_items=quote_count)
    if priming.full_refresh_required or priming.has_more:
        raise RuntimeError("fixture priming produced an incomplete invalidation batch")
    if len(priming.changed_keys) != quote_count:
        raise RuntimeError("fixture priming lost canonical quote identities")

    dependencies = FocusedMirrorDependencyIndex(mirror)
    expected: list[str] = []
    changed_selections = {f"selection-{index}" for index in range(changed_count)}
    for dependency_index in range(dependency_count):
        quote_index = dependency_index % quote_count
        input_id = f"decision-{dependency_index}"
        selection_id = f"selection-{quote_index}"
        dependencies.register(
            input_id,
            source_ids="benchmark",
            event_ids=f"event-{quote_index}",
            market_ids="winner",
            selection_ids=selection_id,
        )
        if selection_id in changed_selections:
            expected.append(input_id)

    if not expected:
        raise RuntimeError("workload must affect at least one registered dependency")
    return _Fixture(
        buffer=buffer,
        dependencies=dependencies,
        expected_affected_ids=tuple(expected),
    )


def _prepare_batch(
    fixture: _Fixture,
    *,
    changed_count: int,
    sequence: int,
):
    for quote_index in range(changed_count):
        fixture.buffer.accept_persisted(
            _event(quote_index=quote_index, sequence=sequence)
        )
    batch = fixture.buffer.drain(max_items=changed_count)
    if batch.full_refresh_required or batch.has_more:
        raise RuntimeError("measured workload produced an incomplete invalidation batch")
    if len(batch.changed_keys) != changed_count:
        raise RuntimeError("measured workload lost changed quote identities")
    return batch


def run_routing_benchmark(
    *,
    quote_count: int = 1_000,
    dependency_count: int = 1_000,
    changed_count: int = 25,
    measured_samples: int = 20,
    warmup_samples: int = 2,
    clock_ns: Clock = perf_counter_ns,
) -> LiveInvalidationRoutingResult:
    """Measure the canonical dirty-key routing + incremental projection hot path.

    Fixture construction, dependency registration and market-event application are
    intentionally outside the timed sections. This isolates the per-tick work that
    follows BoundedMirrorInvalidationBuffer.drain(): routing one bounded batch
    through FocusedMirrorDependencyIndex.affected_inputs() and then reading only
    the affected incremental decision views.

    Results are machine-specific evidence. This harness defines no performance PASS
    threshold and does not imply target-Windows, provider-network or execution speed.
    """

    quote_count = _positive_int("quote_count", quote_count)
    dependency_count = _positive_int("dependency_count", dependency_count)
    changed_count = _positive_int("changed_count", changed_count)
    measured_samples = _positive_int("measured_samples", measured_samples)
    warmup_samples = _nonnegative_int("warmup_samples", warmup_samples)
    if changed_count > quote_count:
        raise ValueError("changed_count must not exceed quote_count")
    if not callable(clock_ns):
        raise TypeError("clock_ns must be callable")

    fixture = _build_fixture(
        quote_count=quote_count,
        dependency_count=dependency_count,
        changed_count=changed_count,
    )
    expected = fixture.expected_affected_ids
    route_samples: list[int] = []
    projection_samples: list[int] = []
    total_samples = warmup_samples + measured_samples

    for sample_index in range(total_samples):
        sequence = sample_index + 2
        batch = _prepare_batch(
            fixture,
            changed_count=changed_count,
            sequence=sequence,
        )

        route_started = clock_ns()
        affected = fixture.dependencies.affected_inputs(batch)
        route_elapsed = clock_ns() - route_started
        if affected != expected:
            raise RuntimeError(
                "invalidation routing returned wrong affected decision identities"
            )

        as_of = _ORIGIN + timedelta(seconds=1)
        projection_started = clock_ns()
        projected_ids: list[str] = []
        for input_id in affected:
            view = fixture.dependencies.incremental_decision_view(
                input_id,
                as_of=as_of,
                max_age=timedelta(minutes=5),
            )
            if len(view.events) != 1:
                raise RuntimeError("incremental decision projection cardinality mismatch")
            projected_ids.append(input_id)
        projection_elapsed = clock_ns() - projection_started
        if tuple(projected_ids) != expected:
            raise RuntimeError("incremental projection omitted an affected decision input")

        if sample_index < warmup_samples:
            continue
        if route_elapsed <= 0 or projection_elapsed <= 0:
            raise RuntimeError("latency clock did not advance for a measured section")
        route_samples.append(route_elapsed)
        projection_samples.append(projection_elapsed)

    if len(route_samples) != measured_samples or len(projection_samples) != measured_samples:
        raise RuntimeError("latency workload did not produce the requested sample count")

    return LiveInvalidationRoutingResult(
        quote_count=quote_count,
        dependency_count=dependency_count,
        changed_count=changed_count,
        measured_samples=measured_samples,
        warmup_samples=warmup_samples,
        expected_affected_count=len(expected),
        dependency_change_pairs=dependency_count * changed_count,
        route_samples_ns=tuple(route_samples),
        projection_samples_ns=tuple(projection_samples),
        route_p50_ms=_nearest_rank_percentile_ms(route_samples, 50),
        route_p95_ms=_nearest_rank_percentile_ms(route_samples, 95),
        route_p99_ms=_nearest_rank_percentile_ms(route_samples, 99),
        route_max_ms=max(route_samples) / 1_000_000.0,
        projection_p50_ms=_nearest_rank_percentile_ms(projection_samples, 50),
        projection_p95_ms=_nearest_rank_percentile_ms(projection_samples, 95),
        projection_p99_ms=_nearest_rank_percentile_ms(projection_samples, 99),
        projection_max_ms=max(projection_samples) / 1_000_000.0,
    )


def run_scaling_suite(
    *,
    quote_count: int,
    dependency_counts: Sequence[int],
    changed_counts: Sequence[int],
    measured_samples: int,
    warmup_samples: int,
    clock_ns: Clock = perf_counter_ns,
) -> tuple[LiveInvalidationRoutingResult, ...]:
    quote_count = _positive_int("quote_count", quote_count)
    if not dependency_counts or not changed_counts:
        raise ValueError("scaling suite dimensions must not be empty")
    normalized_dependencies = tuple(
        _positive_int("dependency_count", value) for value in dependency_counts
    )
    normalized_changes = tuple(
        _positive_int("changed_count", value) for value in changed_counts
    )
    if any(value > quote_count for value in normalized_changes):
        raise ValueError("changed_count must not exceed quote_count")

    return tuple(
        run_routing_benchmark(
            quote_count=quote_count,
            dependency_count=dependency_count,
            changed_count=changed_count,
            measured_samples=measured_samples,
            warmup_samples=warmup_samples,
            clock_ns=clock_ns,
        )
        for dependency_count in normalized_dependencies
        for changed_count in normalized_changes
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Autosport live MarketMirror invalidation routing and affected-only "
            "incremental decision projection. Output is machine-specific evidence, "
            "not a release/performance claim."
        )
    )
    parser.add_argument("--quote-count", type=int, default=1_000)
    parser.add_argument("--dependency-counts", type=int, nargs="+", default=[100, 1_000])
    parser.add_argument("--changed-counts", type=int, nargs="+", default=[1, 25])
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    results = run_scaling_suite(
        quote_count=args.quote_count,
        dependency_counts=args.dependency_counts,
        changed_counts=args.changed_counts,
        measured_samples=args.samples,
        warmup_samples=args.warmup,
    )
    payload = {
        "schema": "autosport.live-invalidation-routing-benchmark",
        "schema_version": 1,
        "scope": "invalidation_routing_plus_affected_incremental_projection",
        "provider_network_included": False,
        "durable_storage_included": False,
        "market_event_apply_included": False,
        "target_machine_required": True,
        "target_claim": False,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "results": [asdict(result) for result in results],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
