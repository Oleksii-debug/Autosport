from __future__ import annotations

import json

import pytest

from autosport.latency_budget import (
    LatencyBudget,
    LatencyBudgetError,
    LatencyMeasurement,
    LatencySummary,
    measure_call,
    summarize_latency,
)


def test_measure_call_uses_injected_monotonic_clock_and_preserves_result() -> None:
    ticks = iter((1_000, 1_250))
    budget = LatencyBudget("quote-to-decision", 300)

    result, measurement = measure_call(
        "decision",
        lambda: "accepted",
        budget=budget,
        clock_ns=lambda: next(ticks),
    )

    assert result == "accepted"
    assert measurement.elapsed_ns == 250
    assert measurement.within_budget
    assert measurement.overrun_ns == 0


def test_exact_budget_boundary_is_compliant_and_one_ns_over_is_breach() -> None:
    budget = LatencyBudget("boundary", 100)

    exact = LatencyMeasurement("exact", 100, budget)
    over = LatencyMeasurement("over", 101, budget)

    assert exact.within_budget
    assert exact.overrun_ns == 0
    assert not over.within_budget
    assert over.overrun_ns == 1


def test_summary_uses_reproducible_nearest_rank_percentiles_and_breach_count() -> None:
    budget = LatencyBudget("batch", 50)
    samples = list(range(1, 101))

    report = summarize_latency(reversed(samples), budget=budget)

    assert report.sample_count == 100
    assert report.total_ns == 5_050
    assert report.min_ns == 1
    assert report.p50_ns == 50
    assert report.p95_ns == 95
    assert report.p99_ns == 99
    assert report.max_ns == 100
    assert report.breach_count == 50
    assert not report.within_budget
    assert report.worst_overrun_ns == 50
    assert report.to_dict()["percentile_method"] == "nearest-rank"


def test_summary_accepts_generator_and_allows_zero_elapsed_sample() -> None:
    budget = LatencyBudget("fast", 10)

    report = summarize_latency((value for value in (0, 5, 10)), budget=budget)

    assert report.within_budget
    assert report.breach_count == 0
    assert report.min_ns == 0
    assert report.max_ns == 10


def test_invalid_budget_and_sample_values_fail_closed() -> None:
    with pytest.raises(LatencyBudgetError, match="positive integer"):
        LatencyBudget("bad", 0)
    with pytest.raises(LatencyBudgetError, match="positive integer"):
        LatencyBudget("bad", True)
    with pytest.raises(LatencyBudgetError, match="canonical string"):
        LatencyBudget(" bad ", 1)

    budget = LatencyBudget("valid", 1)
    with pytest.raises(LatencyBudgetError, match="at least one"):
        summarize_latency([], budget=budget)
    with pytest.raises(LatencyBudgetError, match=r"samples_ns\[1\]"):
        summarize_latency([0, -1], budget=budget)
    with pytest.raises(LatencyBudgetError, match=r"samples_ns\[1\]"):
        summarize_latency([0, True], budget=budget)


def test_summary_constructor_rejects_inconsistent_rank_order() -> None:
    budget = LatencyBudget("forged", 10)

    with pytest.raises(LatencyBudgetError, match="ranks must be monotonic"):
        LatencySummary(
            budget=budget,
            sample_count=2,
            total_ns=3,
            min_ns=1,
            p50_ns=2,
            p95_ns=1,
            p99_ns=2,
            max_ns=2,
            breach_count=0,
        )


def test_summary_constructor_rejects_budget_and_total_contradictions() -> None:
    budget = LatencyBudget("forged", 10)

    with pytest.raises(LatencyBudgetError, match="max_ns budget breach"):
        LatencySummary(
            budget=budget,
            sample_count=2,
            total_ns=21,
            min_ns=10,
            p50_ns=10,
            p95_ns=11,
            p99_ns=11,
            max_ns=11,
            breach_count=0,
        )

    with pytest.raises(LatencyBudgetError, match="total_ns contradicts"):
        LatencySummary(
            budget=budget,
            sample_count=2,
            total_ns=100,
            min_ns=1,
            p50_ns=1,
            p95_ns=2,
            p99_ns=2,
            max_ns=2,
            breach_count=0,
        )


def test_integer_millisecond_constructor_is_exact() -> None:
    assert LatencyBudget.from_milliseconds("ui", 25).budget_ns == 25_000_000
    with pytest.raises(LatencyBudgetError, match="positive integer"):
        LatencyBudget.from_milliseconds("ui", 1.5)  # type: ignore[arg-type]


def test_backward_or_non_integer_clock_fails_closed() -> None:
    budget = LatencyBudget("clock", 10)
    backwards = iter((100, 99))
    with pytest.raises(LatencyBudgetError, match="moved backwards"):
        measure_call(
            "op",
            lambda: None,
            budget=budget,
            clock_ns=lambda: next(backwards),
        )

    bad = iter((100, 101.0))
    with pytest.raises(LatencyBudgetError, match="integer nanoseconds"):
        measure_call(
            "op",
            lambda: None,
            budget=budget,
            clock_ns=lambda: next(bad),  # type: ignore[return-value]
        )


def test_wrapped_exception_propagates_and_truth_flags_remain_false() -> None:
    budget = LatencyBudget("failing-call", 10)
    ticks = iter((1, 2))

    def fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        measure_call("op", fail, budget=budget, clock_ns=lambda: next(ticks))

    payload = summarize_latency([1, 2], budget=budget).to_dict()
    json.dumps(payload, allow_nan=False)
    assert payload["release_authority"] is False
    assert payload["execution_authority"] is False
    assert payload["readiness_authority"] is False
    assert payload["real_hardware_performance_evidence"] is False
    assert payload["whole_product_complete"] is False
