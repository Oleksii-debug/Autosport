from __future__ import annotations

import json
import math

import pytest

from autosport.market_bus import MarketEventBus
from benchmarks.benchmark_market_event_bus_batching import (
    _nearest_rank_ns,
    _nonnegative_int,
    _positive_int,
    main,
    run_batching_profile,
)


def test_nearest_rank_ns_uses_observed_conservative_rank() -> None:
    samples = [4, 1, 3, 2]

    assert _nearest_rank_ns(samples, 50) == 2
    assert _nearest_rank_ns(samples, 95) == 4
    assert _nearest_rank_ns(samples, 99) == 4
    assert _nearest_rank_ns(samples, 100) == 4


@pytest.mark.parametrize("samples", [[], [0], [-1], [True], [1.5]])
def test_nearest_rank_rejects_invalid_samples(samples: list[object]) -> None:
    with pytest.raises(ValueError):
        _nearest_rank_ns(samples, 95)  # type: ignore[arg-type]


@pytest.mark.parametrize("percentile", [True, 0, -1, 101, float("nan"), float("inf")])
def test_nearest_rank_rejects_invalid_percentile(percentile: object) -> None:
    with pytest.raises(ValueError, match="percentile must be a finite number"):
        _nearest_rank_ns([1], percentile)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "4"])
def test_positive_int_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        _positive_int("batch_size", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, True, 1.5, "0"])
def test_nonnegative_int_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        _nonnegative_int("warmup_batches", value)  # type: ignore[arg-type]


def test_small_profile_proves_equivalent_durable_workload() -> None:
    result = run_batching_profile(
        sample_count=4,
        batch_size=3,
        warmup_batches=1,
        quote_keys=2,
    )

    assert result.sample_count == 4
    assert result.batch_size == 3
    assert result.warmup_batches == 1
    assert result.quote_keys == 2
    assert result.accepted_single == 12
    assert result.accepted_batch == 12
    assert len(result.single_samples_ns) == 4
    assert len(result.batch_samples_ns) == 4
    assert all(sample > 0 for sample in result.single_samples_ns)
    assert all(sample > 0 for sample in result.batch_samples_ns)
    assert 0 < result.single_p50_ms_per_event <= result.single_p95_ms_per_event
    assert result.single_p95_ms_per_event <= result.single_p99_ms_per_event
    assert 0 < result.batch_p50_ms_per_event <= result.batch_p95_ms_per_event
    assert result.batch_p95_ms_per_event <= result.batch_p99_ms_per_event
    assert math.isfinite(result.p50_single_over_batch_ratio)
    assert result.p50_single_over_batch_ratio > 0


def test_profile_fails_if_batch_path_reports_incomplete_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = MarketEventBus.publish_many

    def lying_publish_many(self: MarketEventBus, events: object) -> int:
        materialized = tuple(events)  # type: ignore[arg-type]
        accepted = original(self, materialized)
        return accepted - 1

    monkeypatch.setattr(MarketEventBus, "publish_many", lying_publish_many)

    with pytest.raises(RuntimeError, match="batch path accepted wrong batch cardinality"):
        run_batching_profile(
            sample_count=1,
            batch_size=2,
            warmup_batches=0,
            quote_keys=2,
        )


def test_cli_emits_explicit_measurement_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmark_market_event_bus_batching.py",
            "--samples",
            "1",
            "--batch-size",
            "2",
            "--warmup-batches",
            "0",
            "--quote-keys",
            "2",
        ],
    )

    main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "market_event_bus_durable_publication_only"
    assert payload["provider_network_included"] is False
    assert payload["normalization_included"] is False
    assert payload["post_run_truth_verification_included"] is False
    assert payload["pair_order_alternates"] is True
    assert payload["performance_threshold_defined"] is False
    assert payload["target_claim"] is False
    assert payload["accepted_single"] == 2
    assert payload["accepted_batch"] == 2
