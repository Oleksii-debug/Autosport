from __future__ import annotations

from collections.abc import Iterator

import pytest

import benchmarks.benchmark_mirror_dependency_routing as benchmark
from autosport.market_mirror_runtime import FocusedMirrorDependencyIndex
from benchmarks.benchmark_mirror_dependency_routing import (
    MirrorDependencyRoutingLatencyResult,
    _nearest_rank_percentile_ms,
    _nonnegative_int,
    _positive_int,
    run_latency_benchmark,
)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10"])
def test_positive_integer_contract_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        _positive_int("input_count", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, True, 1.5, "10"])
def test_nonnegative_integer_contract_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        _nonnegative_int("warmup_updates", value)  # type: ignore[arg-type]


def test_nearest_rank_percentiles_require_positive_integer_nanoseconds() -> None:
    assert _nearest_rank_percentile_ms([1_000, 2_000, 3_000, 4_000], 50) == 0.002
    assert _nearest_rank_percentile_ms([1_000, 2_000, 3_000, 4_000], 95) == 0.004
    with pytest.raises(ValueError, match="must not be empty"):
        _nearest_rank_percentile_ms([], 95)
    with pytest.raises(ValueError, match="positive integer nanoseconds"):
        _nearest_rank_percentile_ms([0], 95)


def test_benchmark_routes_exact_single_dependency_with_deterministic_clock() -> None:
    clock_values: Iterator[int] = iter(range(1_000, 1_000 + 16 * 10, 10))

    result = run_latency_benchmark(
        input_count=3,
        measured_updates=5,
        warmup_updates=2,
        clock_ns=lambda: next(clock_values),
    )

    assert result.input_count == 3
    assert result.measured_updates == 5
    assert result.warmup_updates == 2
    assert result.affected_per_update == 1
    assert result.p50_ms == pytest.approx(0.00001)
    assert result.p95_ms == pytest.approx(0.00001)
    assert result.p99_ms == pytest.approx(0.00001)
    assert result.max_ms == pytest.approx(0.00001)


def test_benchmark_fails_closed_when_routing_returns_wrong_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        FocusedMirrorDependencyIndex,
        "affected_inputs",
        lambda _self, _batch: ("decision-wrong",),
    )

    with pytest.raises(RuntimeError, match="wrong input identities"):
        run_latency_benchmark(
            input_count=2,
            measured_updates=1,
            warmup_updates=0,
            clock_ns=lambda: 1_000,
        )


def test_benchmark_fails_closed_when_measured_clock_does_not_advance() -> None:
    with pytest.raises(RuntimeError, match="clock did not advance"):
        run_latency_benchmark(
            input_count=1,
            measured_updates=1,
            warmup_updates=0,
            clock_ns=lambda: 1_000,
        )


def test_cli_output_preserves_measurement_truth_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        benchmark,
        "run_latency_benchmark",
        lambda **_kwargs: MirrorDependencyRoutingLatencyResult(
            input_count=1000,
            measured_updates=5000,
            warmup_updates=200,
            affected_per_update=1,
            p50_ms=0.1,
            p95_ms=0.2,
            p99_ms=0.3,
            max_ms=0.4,
        ),
    )
    monkeypatch.setattr("sys.argv", ["benchmark_mirror_dependency_routing.py"])

    benchmark.main()

    output = capsys.readouterr().out.strip()
    assert "scope=market_mirror_dirty_key_to_affected_input_routing" in output
    assert "provider_network_included=false" in output
    assert "persistence_included=false" in output
    assert "mirror_apply_included=false" in output
    assert "portfolio_recompute_included=false" in output
    assert "target_machine_required=true" in output
    assert "target_claim=false" in output
    assert "p95_ms=0.200000" in output
