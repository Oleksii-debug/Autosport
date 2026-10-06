from __future__ import annotations

import math
from datetime import datetime

import pytest

from benchmarks.benchmark_market_mirror_latency import (
    _build_quote,
    _nearest_rank_percentile_ms,
    _nonnegative_int,
    _positive_int,
    run_latency_benchmark,
)


def test_nearest_rank_percentile_uses_conservative_observed_rank() -> None:
    samples_ns = [4_000_000, 1_000_000, 3_000_000, 2_000_000]

    assert _nearest_rank_percentile_ms(samples_ns, 50) == 2.0
    assert _nearest_rank_percentile_ms(samples_ns, 95) == 4.0
    assert _nearest_rank_percentile_ms(samples_ns, 99) == 4.0
    assert _nearest_rank_percentile_ms(samples_ns, 100) == 4.0


@pytest.mark.parametrize("samples", [[], [0], [-1], [True], [1.5]])
def test_percentile_rejects_invalid_latency_samples(samples: list[object]) -> None:
    with pytest.raises(ValueError):
        _nearest_rank_percentile_ms(samples, 95)  # type: ignore[arg-type]


@pytest.mark.parametrize("percentile", [True, 0, -1, 101, float("nan"), float("inf")])
def test_percentile_rejects_invalid_percentile(percentile: object) -> None:
    with pytest.raises(ValueError, match="percentile must be a finite number"):
        _nearest_rank_percentile_ms([1_000_000], percentile)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "5"])
def test_positive_int_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        _positive_int("count", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, True, 1.5, "0"])
def test_nonnegative_int_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        _nonnegative_int("warmup", value)  # type: ignore[arg-type]


def test_quote_fixture_rotates_keys_without_reusing_event_identity_sequence() -> None:
    quotes = [_build_quote(index, quote_keys=2) for index in range(4)]
    instants = [datetime.fromisoformat(quote.observed_ts) for quote in quotes]

    assert [quote.provider_event_id for quote in quotes] == [
        "event-0",
        "event-1",
        "event-0",
        "event-1",
    ]
    assert [quote.sequence for quote in quotes] == [0, 1, 2, 3]
    assert all(left < right for left, right in zip(instants, instants[1:], strict=False))


def test_small_latency_benchmark_reports_complete_positive_ordered_summary() -> None:
    result = run_latency_benchmark(count=7, quote_keys=3, warmup=2)

    assert result.measured_count == 7
    assert result.warmup_count == 2
    assert result.quote_keys == 3
    assert result.accepted == 7
    assert math.isfinite(result.p50_ms) and result.p50_ms > 0
    assert result.p50_ms <= result.p95_ms <= result.p99_ms <= result.max_ms
