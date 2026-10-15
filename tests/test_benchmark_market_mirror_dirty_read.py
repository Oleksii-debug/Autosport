from __future__ import annotations

import math

import pytest

from benchmarks.benchmark_market_mirror_dirty_read import (
    _AS_OF,
    _MAX_AGE,
    _assert_equivalent,
    _build_fixture,
    _nearest_rank_percentile_ms,
    _nonnegative_int,
    _positive_int,
    run_dirty_read_benchmark,
)


def test_fixture_builds_exact_dirty_key_subset_and_equivalent_reads() -> None:
    mirror, keys, selections = _build_fixture(mirror_size=17, dirty_key_count=5)

    full_snapshot = mirror.active_view(
        as_of=_AS_OF,
        max_age=_MAX_AGE,
        selection_ids=selections,
    )
    dirty_snapshot = mirror.active_view_for_keys(
        keys,
        as_of=_AS_OF,
        max_age=_MAX_AGE,
    )

    _assert_equivalent(full_snapshot, dirty_snapshot, expected_count=5)
    assert len(keys) == 5
    assert len(selections) == 5


@pytest.mark.parametrize(
    "name,value",
    [("mirror_size", 0), ("dirty_key_count", -1), ("iterations", True)],
)
def test_positive_int_rejects_invalid_values(name: str, value: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        _positive_int(name, value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, True, 1.5, "0"])
def test_nonnegative_int_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        _nonnegative_int("warmup", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("samples", [[], [0], [-1], [True], [1.5]])
def test_percentile_rejects_invalid_samples(samples: list[object]) -> None:
    with pytest.raises(ValueError):
        _nearest_rank_percentile_ms(samples, 95)  # type: ignore[arg-type]


@pytest.mark.parametrize("percentile", [True, 0, -1, 101, float("nan"), float("inf")])
def test_percentile_rejects_invalid_percentile(percentile: object) -> None:
    with pytest.raises(ValueError, match="percentile must be a finite number"):
        _nearest_rank_percentile_ms([1_000_000], percentile)  # type: ignore[arg-type]


def test_dirty_key_count_cannot_exceed_mirror_size() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        run_dirty_read_benchmark(
            mirror_size=3,
            dirty_key_count=4,
            iterations=1,
            warmup=0,
        )


def test_small_benchmark_reports_parity_and_positive_ordered_measurements() -> None:
    result = run_dirty_read_benchmark(
        mirror_size=31,
        dirty_key_count=4,
        iterations=7,
        warmup=2,
    )

    assert result.mirror_size == 31
    assert result.dirty_key_count == 4
    assert result.measured_iterations == 7
    assert result.warmup_iterations == 2
    assert result.verified_event_count == 4
    assert 0 < result.full_view_p50_ms <= result.full_view_p95_ms
    assert result.full_view_p95_ms <= result.full_view_p99_ms <= result.full_view_max_ms
    assert 0 < result.dirty_view_p50_ms <= result.dirty_view_p95_ms
    assert result.dirty_view_p95_ms <= result.dirty_view_p99_ms <= result.dirty_view_max_ms
    assert math.isfinite(result.p50_dirty_over_full_ratio)
    assert result.p50_dirty_over_full_ratio > 0
