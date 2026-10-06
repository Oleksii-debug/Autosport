#!/usr/bin/env python3
"""Deterministic offline latency evidence for canonical MarketMirror operations.

This benchmark is measurement-only. It sets no latency pass/fail threshold and does
not mutate provider, decision, execution, economic, or release authority.

The timed boundary excludes fixture construction and mirror priming. Batched apply/get
samples include the small caller-side Python iteration and result capture required to
issue each API call; projection samples time one API call including canonical snapshot
cloning. This is an end-to-end in-process API benchmark, not an isolated CPU primitive.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate


_SCHEMA = "autosport.market_mirror_benchmark"
_SCHEMA_VERSION = 1
_BASE_TIME = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)
_MAX_AGE = timedelta(minutes=5)
_SOURCE_ID = "benchmark-provider"
_SPORT = "table_tennis"
_MARKET_ID = "match-winner"


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _source_sha(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 40
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("source_sha must be a lowercase 40-character Git commit SHA")
    return value


def _event(index: int, *, sequence: int, odds: str = "2.00") -> MarketEvent:
    timestamp = _BASE_TIME.isoformat()
    return MarketEvent(
        event_id=f"benchmark-event-{index}",
        market_id=_MARKET_ID,
        selection_id=f"selection-{index}",
        decimal_odds=Decimal(odds),
        observed_ts=timestamp,
        source_id=_SOURCE_ID,
        sequence=sequence,
        status="open",
        source_ts=timestamp,
        ingest_ts=timestamp,
        sport=_SPORT,
    )


def _events(quote_count: int, *, sequence: int, odds: str = "2.00") -> tuple[MarketEvent, ...]:
    return tuple(
        _event(index, sequence=sequence, odds=odds)
        for index in range(quote_count)
    )


def _prime(events: Iterable[MarketEvent]) -> MarketMirror:
    mirror = MarketMirror()
    for event in events:
        result = mirror.apply(event)
        if result.status is not MirrorUpdate.APPLIED:
            raise RuntimeError("MarketMirror benchmark priming did not apply canonical event")
    return mirror


def _nearest_rank(values: list[int], percentile: float) -> int:
    if not values:
        raise ValueError("at least one timing sample is required")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _median(values: list[int]) -> int:
    if not values:
        raise ValueError("at least one timing sample is required")
    ordered = sorted(values)
    count = len(ordered)
    if count % 2:
        return ordered[count // 2]
    return (ordered[(count // 2) - 1] + ordered[count // 2]) // 2


def _summary(
    durations_ns: list[int],
    *,
    operations_per_sample: int,
    outcome_counts: Counter[str] | None = None,
) -> dict[str, object]:
    if not durations_ns:
        raise ValueError("at least one timing sample is required")
    operations = _positive_int(operations_per_sample, "operations_per_sample")
    if any(type(value) is not int or value < 0 for value in durations_ns):
        raise ValueError("timing samples must be non-negative integer nanoseconds")

    per_operation = [value // operations for value in durations_ns]
    result: dict[str, object] = {
        "sample_count": len(durations_ns),
        "operations_per_sample": operations,
        "total_operations": operations * len(durations_ns),
        "batch_min_ns": min(durations_ns),
        "batch_median_ns": _median(durations_ns),
        "batch_p95_ns": _nearest_rank(durations_ns, 0.95),
        "batch_max_ns": max(durations_ns),
        "per_operation_min_ns": min(per_operation),
        "per_operation_median_ns": _median(per_operation),
        "per_operation_p95_ns": _nearest_rank(per_operation, 0.95),
        "per_operation_max_ns": max(per_operation),
        "samples_ns": durations_ns,
    }
    if outcome_counts is not None:
        result["outcome_counts"] = dict(sorted(outcome_counts.items()))
    return result


def _timed(
    operation: Callable[[], Counter[str] | None],
    *,
    samples: int,
    operations_per_sample: int,
    timer_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, object]:
    count = _positive_int(samples, "samples")
    _positive_int(operations_per_sample, "operations_per_sample")
    durations: list[int] = []
    outcomes: Counter[str] = Counter()
    saw_outcomes = False
    for _ in range(count):
        started = timer_ns()
        sample_outcomes = operation()
        finished = timer_ns()
        elapsed = finished - started
        if elapsed < 0:
            raise RuntimeError("monotonic benchmark timer moved backwards")
        durations.append(elapsed)
        if sample_outcomes is not None:
            saw_outcomes = True
            outcomes.update(sample_outcomes)
    return _summary(
        durations,
        operations_per_sample=operations_per_sample,
        outcome_counts=outcomes if saw_outcomes else None,
    )


def _scenario_distinct_insert(
    base_events: tuple[MarketEvent, ...],
    *,
    samples: int,
    timer_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, object]:
    quote_count = len(base_events)
    _positive_int(quote_count, "quote_count")
    mirrors = iter(tuple(MarketMirror() for _ in range(samples)))

    def operation() -> Counter[str]:
        mirror = next(mirrors)
        statuses: Counter[str] = Counter()
        for event in base_events:
            statuses[mirror.apply(event).status.value] += 1
        if statuses != Counter({MirrorUpdate.APPLIED.value: quote_count}):
            raise RuntimeError("distinct insert benchmark observed non-APPLIED result")
        return statuses

    return _timed(
        operation,
        samples=samples,
        operations_per_sample=quote_count,
        timer_ns=timer_ns,
    )


def _scenario_forward_update(
    base_events: tuple[MarketEvent, ...],
    *,
    samples: int,
    timer_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, object]:
    quote_count = len(base_events)
    mirror = _prime(base_events)
    batches = iter(
        tuple(
            _events(
                quote_count,
                sequence=sequence,
                odds="2.01" if sequence % 2 else "2.02",
            )
            for sequence in range(2, samples + 2)
        )
    )

    def operation() -> Counter[str]:
        statuses: Counter[str] = Counter()
        for event in next(batches):
            statuses[mirror.apply(event).status.value] += 1
        if statuses != Counter({MirrorUpdate.APPLIED.value: quote_count}):
            raise RuntimeError("forward update benchmark observed non-APPLIED result")
        return statuses

    return _timed(
        operation,
        samples=samples,
        operations_per_sample=quote_count,
        timer_ns=timer_ns,
    )


def _scenario_exact_lookup(
    base_events: tuple[MarketEvent, ...],
    *,
    samples: int,
    timer_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, object]:
    quote_count = len(base_events)
    mirror = _prime(base_events)

    def operation() -> Counter[str]:
        found = 0
        for event in base_events:
            restored = mirror.get(
                event.source_id,
                event.event_id,
                event.market_id,
                event.selection_id,
                sport=event.sport,
            )
            if restored is not None:
                found += 1
        if found != quote_count:
            raise RuntimeError("exact lookup benchmark failed to restore every quote")
        return Counter({"found": found})

    return _timed(
        operation,
        samples=samples,
        operations_per_sample=quote_count,
        timer_ns=timer_ns,
    )


def _scenario_focused_projection(
    base_events: tuple[MarketEvent, ...],
    *,
    samples: int,
    focused_key_count: int,
    timer_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, object]:
    quote_count = len(base_events)
    selected = min(_positive_int(focused_key_count, "focused_key_count"), quote_count)
    mirror = _prime(base_events)
    keys = tuple(
        (event.source_id, event.quote_key)
        for event in base_events[:selected]
    )

    def operation() -> Counter[str]:
        snapshot = mirror.active_view_for_keys(
            keys,
            as_of=_BASE_TIME,
            max_age=_MAX_AGE,
        )
        if len(snapshot.events) != selected:
            raise RuntimeError("focused projection benchmark lost eligible quote evidence")
        return Counter({"eligible": len(snapshot.events)})

    return _timed(
        operation,
        samples=samples,
        operations_per_sample=1,
        timer_ns=timer_ns,
    )


def _scenario_full_snapshot(
    base_events: tuple[MarketEvent, ...],
    *,
    samples: int,
    timer_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, object]:
    quote_count = len(base_events)
    mirror = _prime(base_events)

    def operation() -> Counter[str]:
        snapshot = mirror.snapshot()
        if len(snapshot) != quote_count:
            raise RuntimeError("full snapshot benchmark lost canonical quote evidence")
        return Counter({"snapshotted": len(snapshot)})

    return _timed(
        operation,
        samples=samples,
        operations_per_sample=1,
        timer_ns=timer_ns,
    )


def run_benchmark(
    *,
    source_sha: str,
    quote_count: int = 256,
    samples: int = 50,
    focused_key_count: int = 16,
    timer_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, object]:
    source = _source_sha(source_sha)
    quotes = _positive_int(quote_count, "quote_count")
    sample_count = _positive_int(samples, "samples")
    focused = _positive_int(focused_key_count, "focused_key_count")
    if focused > quotes:
        raise ValueError("focused_key_count must not exceed quote_count")

    base_events = _events(quotes, sequence=1)
    timer = time.get_clock_info("perf_counter")
    return {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "source_sha": source,
        "measurement_boundary": (
            "In-memory MarketMirror API boundary. Event construction, fixture "
            "generation, and mirror priming are excluded. Batched apply/get timings "
            "include caller-side Python iteration and result capture needed to issue "
            "the calls; projection timings cover one API call including canonical "
            "snapshot cloning. No network, disk, provider polling, decision engine, "
            "execution, or sleep is included."
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
            "quote_count": quotes,
            "samples": sample_count,
            "focused_key_count": focused,
            "sport": _SPORT,
            "source_count": 1,
            "market_count": 1,
        },
        "scenarios": {
            "distinct_insert_apply": _scenario_distinct_insert(
                base_events,
                samples=sample_count,
                timer_ns=timer_ns,
            ),
            "forward_update_apply": _scenario_forward_update(
                base_events,
                samples=sample_count,
                timer_ns=timer_ns,
            ),
            "exact_source_sport_lookup": _scenario_exact_lookup(
                base_events,
                samples=sample_count,
                timer_ns=timer_ns,
            ),
            "focused_active_projection": _scenario_focused_projection(
                base_events,
                samples=sample_count,
                focused_key_count=focused,
                timer_ns=timer_ns,
            ),
            "full_snapshot_projection": _scenario_full_snapshot(
                base_events,
                samples=sample_count,
                timer_ns=timer_ns,
            ),
        },
    }


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
    try:
        return _source_sha(head)
    except ValueError as exc:
        raise RuntimeError("git rev-parse HEAD did not return a canonical commit SHA") from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure canonical in-memory MarketMirror latency without setting a "
            "performance pass/fail threshold."
        )
    )
    parser.add_argument("--quotes", type=int, default=256)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--focused-keys", type=int, default=16)
    parser.add_argument(
        "--json-out",
        type=Path,
        help="optional canonical JSON evidence path; stdout is always emitted",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    report = run_benchmark(
        source_sha=_clean_source_sha(repo_root),
        quote_count=args.quotes,
        samples=args.samples,
        focused_key_count=args.focused_keys,
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
