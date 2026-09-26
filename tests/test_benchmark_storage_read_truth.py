from __future__ import annotations

from dataclasses import replace

import pytest

from benchmarks.benchmark_storage_read import (
    StorageReadBenchmarkResult,
    _build_events,
    _positive_int,
    run_benchmark,
)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "100"])
def test_storage_read_benchmark_rejects_invalid_workload_size(value: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        _positive_int("count", value)  # type: ignore[arg-type]


def test_storage_read_fixture_exercises_projection_overwrite_and_event_filter() -> None:
    events = _build_events(102)

    assert len(events) == 102
    assert len({event.dedupe_key for event in events}) == 102
    assert len({(event.source_id, event.quote_key) for event in events}) == 100
    assert sum(event.event_id == "event-0" for event in events) == 3


@pytest.mark.parametrize(
    "field",
    [
        "reopen_seconds",
        "current_read_seconds",
        "filtered_event_read_seconds",
        "full_history_read_seconds",
    ],
)
@pytest.mark.parametrize("invalid", [0.0, -1.0, float("nan"), float("inf")])
def test_storage_read_result_rejects_invalid_timing_evidence(
    field: str,
    invalid: float,
) -> None:
    valid = StorageReadBenchmarkResult(
        requested_history_rows=102,
        history_rows=102,
        current_projection_rows=100,
        filtered_event_id="event-0",
        filtered_event_rows=3,
        reopen_seconds=0.01,
        current_read_seconds=0.01,
        filtered_event_read_seconds=0.01,
        full_history_read_seconds=0.01,
    )

    with pytest.raises(ValueError, match="finite and positive"):
        replace(valid, **{field: invalid})


def test_small_storage_read_benchmark_reports_exact_workload_truth() -> None:
    result = run_benchmark(count=102)

    assert result.requested_history_rows == 102
    assert result.history_rows == 102
    assert result.current_projection_rows == 100
    assert result.filtered_event_id == "event-0"
    assert result.filtered_event_rows == 3
    assert result.reopen_seconds > 0
    assert result.current_read_seconds > 0
    assert result.filtered_event_read_seconds > 0
    assert result.full_history_read_seconds > 0
    assert result.filtered_event_rows_per_second > 0
    assert result.full_history_rows_per_second > 0
