from __future__ import annotations

import math
from datetime import datetime

import pytest

from benchmarks.benchmark_replay_event_driven import (
    _build_events,
    _positive_elapsed_seconds,
    _positive_int,
    _source_duration_seconds,
    run_replay_benchmark,
)


@pytest.mark.parametrize(
    ("value", "minimum"),
    [
        (0, 1),
        (-1, 1),
        (1, 2),
        (True, 1),
        (1.0, 1),
        ("2", 1),
    ],
)
def test_positive_int_rejects_invalid_values(value: object, minimum: int) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        _positive_int("count", value, minimum=minimum)


@pytest.mark.parametrize(
    ("start_ns", "end_ns"),
    [
        (10, 10),
        (11, 10),
        (True, 20),
        (10, False),
        (1.0, 20),
        (10, 20.0),
    ],
)
def test_positive_elapsed_seconds_rejects_invalid_clock_samples(
    start_ns: object,
    end_ns: object,
) -> None:
    with pytest.raises(ValueError):
        _positive_elapsed_seconds(  # type: ignore[arg-type]
            start_ns,
            end_ns,
            "replay_elapsed_seconds",
        )


def test_build_events_is_strictly_chronological_and_dedupe_unique() -> None:
    events = _build_events(205, 250)
    instants = [datetime.fromisoformat(event.observed_ts) for event in events]

    assert all(left < right for left, right in zip(instants, instants[1:], strict=False))
    assert len({event.dedupe_key for event in events}) == 205
    assert events[0].sequence == 1
    assert events[-1].sequence == 205
    assert events[0].source_id == "replay-benchmark"


def test_source_duration_uses_recording_span_not_event_count() -> None:
    events = _build_events(4, 250)

    assert _source_duration_seconds(events) == 0.75


def test_small_replay_benchmark_reports_truthful_durable_event_driven_scope() -> None:
    result = run_replay_benchmark(count=8, interval_ms=1_000)

    assert result.event_count == 8
    assert result.accepted_events == 8
    assert result.durable_history_events == 8
    assert result.source_duration_seconds == 7.0
    assert result.mode == "fastest-event-driven"
    assert result.consumer_scope == "sqlite-market-store"
    assert result.provider_network_included is False
    assert result.agent_callbacks_included is False
    assert result.target_claim is False
    assert len(result.replay_dataset_hash) == 64
    assert math.isfinite(result.prepare_elapsed_seconds) and result.prepare_elapsed_seconds > 0
    assert math.isfinite(result.replay_elapsed_seconds) and result.replay_elapsed_seconds > 0
    assert math.isfinite(result.events_per_second) and result.events_per_second > 0
    assert (
        math.isfinite(result.equivalent_realtime_multiplier)
        and result.equivalent_realtime_multiplier > 0
    )
