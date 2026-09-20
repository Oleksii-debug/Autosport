from dataclasses import replace

import pytest

from autosport.performance_qualification import (
    MetricQualification,
    PerformanceBudget,
    PerformanceQualification,
    PerformanceQualificationError,
)


_SOURCE_SHA = "a" * 40
_REPORT_SHA256 = "b" * 64


def _check(
    metric: str,
    comparator: str,
    observed: int | float,
    threshold: int | float,
) -> MetricQualification:
    passed = observed >= threshold if comparator == ">=" else observed <= threshold
    return MetricQualification(
        metric=metric,
        comparator=comparator,
        observed=observed,
        threshold=threshold,
        status="PASS" if passed else "FAIL",
    )


def _qualification(
    budget: PerformanceBudget,
    checks: tuple[MetricQualification, ...],
) -> PerformanceQualification:
    return PerformanceQualification(
        source_sha=_SOURCE_SHA,
        machine_profile="test-machine",
        report_sha256=_REPORT_SHA256,
        budget=budget,
        checks=checks,
    )


def test_rejects_unconstrained_pass_metric() -> None:
    budget = PerformanceBudget(min_history_events=100)
    forged = _check("caller_pass", ">=", 1, 1)

    with pytest.raises(PerformanceQualificationError, match="unconstrained"):
        _qualification(budget, (forged,))


def test_rejects_omitted_budget_check() -> None:
    budget = PerformanceBudget(
        min_history_events=100,
        max_peak_traced_memory_bytes=1024,
    )
    history = _check("history_events", ">=", 150, 100)

    with pytest.raises(PerformanceQualificationError, match="exactly cover"):
        _qualification(budget, (history,))


def test_rejects_duplicate_metric() -> None:
    budget = PerformanceBudget(min_history_events=100)
    history = _check("history_events", ">=", 150, 100)

    with pytest.raises(PerformanceQualificationError, match="duplicate"):
        _qualification(budget, (history, history))


def test_rejects_wrong_comparator_even_when_check_passes() -> None:
    budget = PerformanceBudget(min_history_events=100)
    forged = _check("history_events", "<=", 50, 100)

    with pytest.raises(PerformanceQualificationError, match="comparator"):
        _qualification(budget, (forged,))


def test_rejects_wrong_threshold_even_when_check_passes() -> None:
    budget = PerformanceBudget(min_history_events=100)
    forged = _check("history_events", ">=", 150, 1)

    with pytest.raises(PerformanceQualificationError, match="threshold"):
        _qualification(budget, (forged,))


def test_dataclasses_replace_cannot_mint_pass_with_forged_checks() -> None:
    budget = PerformanceBudget(min_history_events=100)
    genuine = _qualification(
        budget,
        (_check("history_events", ">=", 150, 100),),
    )
    forged = _check("caller_pass", ">=", 1, 1)

    with pytest.raises(PerformanceQualificationError, match="unconstrained"):
        replace(genuine, checks=(forged,))
