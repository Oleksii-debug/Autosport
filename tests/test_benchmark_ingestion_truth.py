from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from benchmarks.benchmark_ingestion import (
    BenchmarkResult,
    _build_quotes,
    _positive_int,
    run_benchmark,
)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1000"])
def test_benchmark_rejects_non_positive_integer_workload_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        _positive_int("count", value)  # type: ignore[arg-type]


def test_benchmark_timestamps_remain_strictly_monotonic_past_one_hour() -> None:
    quotes = _build_quotes(3_602)
    instants = [datetime.fromisoformat(quote.observed_ts) for quote in quotes]

    assert all(left < right for left, right in zip(instants, instants[1:], strict=False))
    assert instants[1] - instants[0] == timedelta(seconds=1)
    assert instants[3_600] - instants[0] == timedelta(hours=1)
    assert instants[3_600] > instants[3_599]


@pytest.mark.parametrize("elapsed", [0.0, -1.0, float("nan"), float("inf")])
def test_benchmark_refuses_invalid_elapsed_time_for_throughput(elapsed: float) -> None:
    result = BenchmarkResult(
        requested=1,
        received=1,
        accepted=1,
        rejected=0,
        elapsed_seconds=elapsed,
    )

    with pytest.raises(ValueError, match="elapsed_seconds must be finite and positive"):
        _ = result.accepted_per_second


def test_small_benchmark_reports_only_a_fully_accepted_workload() -> None:
    result = run_benchmark(count=7, batch_size=3)

    assert result.requested == 7
    assert result.received == 7
    assert result.accepted == 7
    assert result.rejected == 0
    assert result.elapsed_seconds > 0
    assert result.accepted_per_second > 0
