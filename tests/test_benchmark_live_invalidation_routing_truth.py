from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

import benchmarks.benchmark_live_invalidation_routing as benchmark
from benchmarks.benchmark_live_invalidation_routing import (
    _build_fixture,
    _nearest_rank_percentile_ms,
    _nonnegative_int,
    _positive_int,
    run_routing_benchmark,
    run_scaling_suite,
)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10"])
def test_positive_int_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        _positive_int("count", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, True, 1.5, "0"])
def test_nonnegative_int_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        _nonnegative_int("warmup", value)  # type: ignore[arg-type]


def test_nearest_rank_percentile_preserves_observed_sample() -> None:
    samples = [4_000_000, 1_000_000, 3_000_000, 2_000_000]
    assert _nearest_rank_percentile_ms(samples, 50) == 2.0
    assert _nearest_rank_percentile_ms(samples, 95) == 4.0
    with pytest.raises(ValueError, match="samples must not be empty"):
        _nearest_rank_percentile_ms([], 95)
    with pytest.raises(ValueError, match="positive integer nanoseconds"):
        _nearest_rank_percentile_ms([0], 95)


def test_fixture_routes_exact_dependency_identities_for_changed_quotes() -> None:
    fixture = _build_fixture(quote_count=4, dependency_count=10, changed_count=2)

    assert fixture.expected_affected_ids == (
        "decision-0",
        "decision-1",
        "decision-4",
        "decision-5",
        "decision-8",
        "decision-9",
    )


def test_small_benchmark_reports_exact_workload_and_raw_positive_samples() -> None:
    values: Iterator[int] = iter(range(1_000, 1_000 + 16 * 10, 10))
    result = run_routing_benchmark(
        quote_count=4,
        dependency_count=6,
        changed_count=2,
        measured_samples=3,
        warmup_samples=1,
        clock_ns=lambda: next(values),
    )

    assert result.quote_count == 4
    assert result.dependency_count == 6
    assert result.changed_count == 2
    assert result.expected_affected_count == 4
    assert result.dependency_change_pairs == 12
    assert result.route_samples_ns == (10, 10, 10)
    assert result.projection_samples_ns == (10, 10, 10)
    assert result.route_p50_ms == pytest.approx(0.00001)
    assert result.route_p95_ms == pytest.approx(0.00001)
    assert result.route_p99_ms == pytest.approx(0.00001)
    assert result.route_max_ms == pytest.approx(0.00001)
    assert result.projection_p50_ms == pytest.approx(0.00001)
    assert result.projection_p95_ms == pytest.approx(0.00001)
    assert result.projection_p99_ms == pytest.approx(0.00001)
    assert result.projection_max_ms == pytest.approx(0.00001)


def test_benchmark_fails_closed_when_route_identity_is_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        benchmark.FocusedMirrorDependencyIndex,
        "affected_inputs",
        lambda self, batch: ("decision-wrong",),
    )
    values: Iterator[int] = iter(range(1_000, 1_200, 10))

    with pytest.raises(RuntimeError, match="wrong affected decision identities"):
        run_routing_benchmark(
            quote_count=2,
            dependency_count=2,
            changed_count=1,
            measured_samples=1,
            warmup_samples=0,
            clock_ns=lambda: next(values),
        )


def test_benchmark_fails_closed_when_clock_does_not_advance() -> None:
    with pytest.raises(RuntimeError, match="clock did not advance"):
        run_routing_benchmark(
            quote_count=2,
            dependency_count=2,
            changed_count=1,
            measured_samples=1,
            warmup_samples=0,
            clock_ns=lambda: 1_000,
        )


def test_scaling_suite_crosses_dependency_and_change_dimensions() -> None:
    values: Iterator[int] = iter(range(1_000, 20_000, 10))
    results = run_scaling_suite(
        quote_count=4,
        dependency_counts=(2, 4),
        changed_counts=(1, 2),
        measured_samples=1,
        warmup_samples=0,
        clock_ns=lambda: next(values),
    )

    assert tuple((r.dependency_count, r.changed_count) for r in results) == (
        (2, 1),
        (2, 2),
        (4, 1),
        (4, 2),
    )
    assert all(r.dependency_change_pairs == r.dependency_count * r.changed_count for r in results)


def test_scaling_suite_rejects_change_cardinality_larger_than_quote_universe() -> None:
    with pytest.raises(ValueError, match="must not exceed quote_count"):
        run_scaling_suite(
            quote_count=2,
            dependency_counts=(2,),
            changed_counts=(3,),
            measured_samples=1,
            warmup_samples=0,
        )


def test_cli_emits_machine_readable_truth_boundary(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        benchmark,
        "run_scaling_suite",
        lambda **_kwargs: (
            benchmark.LiveInvalidationRoutingResult(
                quote_count=4,
                dependency_count=6,
                changed_count=2,
                measured_samples=1,
                warmup_samples=0,
                expected_affected_count=4,
                dependency_change_pairs=12,
                route_samples_ns=(1_000,),
                projection_samples_ns=(2_000,),
                route_p50_ms=0.001,
                route_p95_ms=0.001,
                route_p99_ms=0.001,
                route_max_ms=0.001,
                projection_p50_ms=0.002,
                projection_p95_ms=0.002,
                projection_p99_ms=0.002,
                projection_max_ms=0.002,
            ),
        ),
    )
    monkeypatch.setattr("sys.argv", ["benchmark_live_invalidation_routing.py"])

    benchmark.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "autosport.live-invalidation-routing-benchmark"
    assert payload["scope"] == "invalidation_routing_plus_affected_incremental_projection"
    assert payload["provider_network_included"] is False
    assert payload["durable_storage_included"] is False
    assert payload["market_event_apply_included"] is False
    assert payload["target_machine_required"] is True
    assert payload["target_claim"] is False
    assert payload["results"][0]["dependency_change_pairs"] == 12
