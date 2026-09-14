from __future__ import annotations

from collections.abc import Iterator

import pytest

from autosport.portfolio import PortfolioEngine
from benchmarks.benchmark_portfolio_recompute_latency import (
    _build_portfolio,
    _nearest_rank_percentile_ms,
    _nonnegative_int,
    _positive_int,
    run_latency_benchmark,
)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10"])
def test_positive_integer_contract_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a positive integer"):
        _positive_int("count", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [-1, True, 1.5, "10"])
def test_nonnegative_integer_contract_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be a non-negative integer"):
        _nonnegative_int("warmup", value)  # type: ignore[arg-type]


def test_fixture_has_disjoint_deterministic_affected_subgraphs() -> None:
    tickets, triggers = _build_portfolio(event_count=3, tickets_per_event=4)
    engine = PortfolioEngine()

    assert len(tickets) == 12
    assert len(triggers) == 3
    assert len(set(triggers)) == 3
    affected_sets = [set(engine.affected_tickets(tickets, trigger)) for trigger in triggers]
    assert all(len(affected) == 4 for affected in affected_sets)
    assert affected_sets[0].isdisjoint(affected_sets[1])
    assert affected_sets[0].isdisjoint(affected_sets[2])
    assert affected_sets[1].isdisjoint(affected_sets[2])


def test_nearest_rank_percentiles_use_positive_nanosecond_samples() -> None:
    assert _nearest_rank_percentile_ms([1_000_000, 2_000_000, 3_000_000, 4_000_000], 50) == 2.0
    assert _nearest_rank_percentile_ms([1_000_000, 2_000_000, 3_000_000, 4_000_000], 95) == 4.0
    with pytest.raises(ValueError, match="samples must not be empty"):
        _nearest_rank_percentile_ms([], 95)
    with pytest.raises(ValueError, match="positive integer nanoseconds"):
        _nearest_rank_percentile_ms([0], 95)


def test_benchmark_fails_closed_when_measured_clock_does_not_advance() -> None:
    def frozen_clock() -> int:
        return 1_000

    with pytest.raises(RuntimeError, match="clock did not advance"):
        run_latency_benchmark(
            event_count=1,
            tickets_per_event=2,
            measured_updates=1,
            warmup_updates=0,
            clock_ns=frozen_clock,
        )


def test_small_benchmark_reports_complete_exact_affected_workload() -> None:
    values: Iterator[int] = iter(range(1_000, 1_000 + 14 * 10, 10))

    result = run_latency_benchmark(
        event_count=3,
        tickets_per_event=4,
        measured_updates=7,
        warmup_updates=2,
        clock_ns=lambda: next(values),
    )

    assert result.total_tickets == 12
    assert result.event_count == 3
    assert result.tickets_per_event == 4
    assert result.measured_updates == 7
    assert result.warmup_updates == 2
    assert result.affected_tickets_per_update == 4
    assert result.scenarios_per_update == 16
    assert result.p50_ms == pytest.approx(0.00001)
    assert result.p95_ms == pytest.approx(0.00001)
    assert result.p99_ms == pytest.approx(0.00001)
    assert result.max_ms == pytest.approx(0.00001)
