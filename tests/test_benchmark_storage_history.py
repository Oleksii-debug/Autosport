from __future__ import annotations

import json

import pytest

from benchmarks.benchmark_storage_history import (
    OperationMeasurement,
    _build_events,
    _positive_int,
    run_benchmark,
)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1000"])
def test_storage_benchmark_rejects_non_positive_integer_workload_values(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        _positive_int("count", value)  # type: ignore[arg-type]


def test_storage_benchmark_workload_has_stable_repeating_quote_identity() -> None:
    events = _build_events(
        12,
        event_count=3,
        selections_per_event=2,
    )

    assert len(events) == 12
    assert [event.sequence for event in events] == list(range(1, 13))
    assert len({event.event_id for event in events}) == 3
    assert len({event.quote_key for event in events}) == 6
    assert events[0].quote_key == events[6].quote_key
    assert events[0].observed_ts < events[-1].observed_ts


@pytest.mark.parametrize("elapsed", [0.0, -1.0, float("nan"), float("inf")])
def test_storage_benchmark_refuses_invalid_timing_evidence(elapsed: float) -> None:
    measurement = OperationMeasurement(
        operation="read",
        rows=1,
        elapsed_seconds=elapsed,
    )

    with pytest.raises(ValueError, match="elapsed_seconds must be finite and positive"):
        _ = measurement.rows_per_second


def test_storage_benchmark_small_run_proves_all_read_cardinalities() -> None:
    result = run_benchmark(
        count=24,
        event_count=3,
        selections_per_event=2,
    )

    assert result.schema_version == 1
    assert result.requested_events == 24
    assert result.persisted_events == 24
    assert result.event_count == 3
    assert result.selections_per_event == 2
    assert result.target_event_id == "event-0"
    assert result.database_bytes > 0
    assert result.python_version
    assert result.sqlite_version
    assert result.operating_system
    assert result.read_after_reopen is True
    assert result.write.rows == 24
    assert result.full_history_read.rows == 24
    assert result.event_history_read.rows == 8
    assert result.current_projection_read.rows == 6

    for measurement in (
        result.write,
        result.full_history_read,
        result.event_history_read,
        result.current_projection_read,
    ):
        assert measurement.elapsed_seconds > 0
        assert measurement.rows_per_second > 0


def test_storage_benchmark_result_is_machine_readable_without_threshold_claims() -> None:
    result = run_benchmark(
        count=12,
        event_count=3,
        selections_per_event=2,
    )
    payload = result.to_dict()

    serialized = json.dumps(payload, sort_keys=True)
    decoded = json.loads(serialized)
    assert decoded["schema_version"] == 1
    assert decoded["requested_events"] == 12
    assert decoded["event_count"] == 3
    assert decoded["selections_per_event"] == 2
    assert decoded["python_version"]
    assert decoded["sqlite_version"]
    assert decoded["operating_system"]
    assert decoded["read_after_reopen"] is True
    assert "threshold" not in serialized.lower()
    assert payload["full_history_read"]["rows"] == 12
