from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import pytest

from benchmarks.benchmark_replay_event_driven import (
    _build_events,
    _finite_positive_metric,
    _positive_elapsed_seconds,
    _positive_int,
    _source_duration_seconds,
    run_replay_benchmark,
    run_replay_dataset_benchmark,
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
            "dispatch_elapsed_seconds",
        )


@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf")])
def test_finite_positive_metric_rejects_invalid_evidence(value: float) -> None:
    with pytest.raises(RuntimeError, match="invalid throughput"):
        _finite_positive_metric("throughput", value)


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
    events = [events[2], events[0], events[3], events[1]]

    assert _source_duration_seconds(events) == 0.75


def test_small_synthetic_benchmark_is_explicitly_non_release_evidence() -> None:
    result = run_replay_benchmark(count=8, interval_ms=1_000)

    assert result.event_count == 8
    assert result.accepted_events == 8
    assert result.durable_history_events == 8
    assert result.input_mode == "synthetic"
    assert result.source_duration_seconds == 7.0
    assert result.recording_span_is_synthetic is True
    assert result.recording_span_source == "synthetic-fixture-cadence"
    assert result.input_load_elapsed_seconds is None
    assert result.measured_input_pipeline_elapsed_seconds is None
    assert result.measured_input_pipeline_events_per_second is None
    assert result.measured_input_pipeline_realtime_multiplier is None
    assert result.dataset_name is None
    assert result.dataset_schema_version is None
    assert result.dataset_market_sha256 is None
    assert result.dataset_import_identity is None
    assert result.governed_dataset_input is False
    assert result.mode == "fastest-event-driven"
    assert result.consumer_scope == "sqlite-market-store"
    assert result.fixture_construction_included is False
    assert result.sqlite_store_open_included is False
    assert result.provider_network_included is False
    assert result.agent_callbacks_included is False
    assert result.target_claim is False
    assert len(result.replay_dataset_hash) == 64

    assert (
        math.isfinite(result.engine_prepare_elapsed_seconds)
        and result.engine_prepare_elapsed_seconds > 0
    )
    assert math.isfinite(result.dispatch_elapsed_seconds) and result.dispatch_elapsed_seconds > 0
    assert result.measured_engine_total_elapsed_seconds == pytest.approx(
        result.engine_prepare_elapsed_seconds + result.dispatch_elapsed_seconds
    )
    assert result.measured_engine_total_elapsed_seconds > result.dispatch_elapsed_seconds

    assert math.isfinite(result.dispatch_events_per_second)
    assert result.dispatch_events_per_second > 0
    assert math.isfinite(result.measured_engine_total_events_per_second)
    assert 0 < result.measured_engine_total_events_per_second < result.dispatch_events_per_second

    assert math.isfinite(result.dispatch_realtime_multiplier)
    assert result.dispatch_realtime_multiplier > 0
    assert math.isfinite(result.measured_engine_total_realtime_multiplier)
    assert (
        0
        < result.measured_engine_total_realtime_multiplier
        < result.dispatch_realtime_multiplier
    )


def test_canonical_dataset_mode_binds_input_identity_without_recording_provenance_claim() -> None:
    dataset_root = Path(__file__).resolve().parents[1] / "examples" / "tt_demo"

    result = run_replay_dataset_benchmark(dataset_root)

    assert result.input_mode == "canonical-dataset"
    assert result.recording_span_is_synthetic is None
    assert result.recording_span_source == "dataset-observed-timestamps"
    assert result.event_count == result.accepted_events == result.durable_history_events
    assert result.event_count >= 2
    assert result.source_duration_seconds > 0
    assert result.dataset_name == "table-tennis-demo-v1"
    assert result.dataset_schema_version == 1
    assert result.dataset_market_sha256 == (
        "33553b5e0c144997da51ba4555521a6636331ef8e315af0fc78a670e5a32742a"
    )
    assert result.dataset_import_identity is None
    assert result.governed_dataset_input is False
    assert result.target_claim is False
    assert len(result.replay_dataset_hash) == 64

    assert result.input_load_elapsed_seconds is not None
    assert result.input_load_elapsed_seconds > 0
    assert result.measured_input_pipeline_elapsed_seconds is not None
    assert result.measured_input_pipeline_elapsed_seconds == pytest.approx(
        result.input_load_elapsed_seconds + result.measured_engine_total_elapsed_seconds
    )
    assert (
        result.measured_input_pipeline_elapsed_seconds
        > result.measured_engine_total_elapsed_seconds
        > result.dispatch_elapsed_seconds
    )

    assert result.measured_input_pipeline_events_per_second is not None
    assert (
        0
        < result.measured_input_pipeline_events_per_second
        < result.measured_engine_total_events_per_second
        < result.dispatch_events_per_second
    )
    assert result.measured_input_pipeline_realtime_multiplier is not None
    assert (
        0
        < result.measured_input_pipeline_realtime_multiplier
        < result.measured_engine_total_realtime_multiplier
        < result.dispatch_realtime_multiplier
    )
