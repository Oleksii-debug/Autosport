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


def _validated(
    budget: PerformanceBudget,
    checks: tuple[MetricQualification, ...],
) -> PerformanceQualification:
    return PerformanceQualification._from_validated(
        source_sha=_SOURCE_SHA,
        machine_profile="test-machine",
        report_sha256=_REPORT_SHA256,
        budget=budget,
        checks=checks,
    )


def test_public_constructor_cannot_mint_qualification_evidence() -> None:
    budget = PerformanceBudget(min_history_events=100)
    check = _check("history_events", ">=", 150, 100)

    with pytest.raises(PerformanceQualificationError, match="qualify_endurance_report"):
        PerformanceQualification(
            source_sha=_SOURCE_SHA,
            machine_profile="test-machine",
            report_sha256=_REPORT_SHA256,
            budget=budget,
            checks=(check,),
        )


def test_internal_validation_rejects_unconstrained_pass_metric() -> None:
    budget = PerformanceBudget(min_history_events=100)
    forged = _check("caller_pass", ">=", 1, 1)

    with pytest.raises(PerformanceQualificationError, match="unconstrained"):
        _validated(budget, (forged,))


def test_internal_validation_rejects_omitted_budget_check() -> None:
    budget = PerformanceBudget(
        min_history_events=100,
        max_peak_traced_memory_bytes=1024,
    )
    history = _check("history_events", ">=", 150, 100)

    with pytest.raises(PerformanceQualificationError, match="exactly cover"):
        _validated(budget, (history,))


def test_internal_validation_rejects_duplicate_metric() -> None:
    budget = PerformanceBudget(min_history_events=100)
    history = _check("history_events", ">=", 150, 100)

    with pytest.raises(PerformanceQualificationError, match="duplicate"):
        _validated(budget, (history, history))


def test_internal_validation_rejects_wrong_comparator() -> None:
    budget = PerformanceBudget(min_history_events=100)
    forged = _check("history_events", "<=", 50, 100)

    with pytest.raises(PerformanceQualificationError, match="comparator"):
        _validated(budget, (forged,))


def test_internal_validation_rejects_wrong_threshold() -> None:
    budget = PerformanceBudget(min_history_events=100)
    forged = _check("history_events", ">=", 150, 1)

    with pytest.raises(PerformanceQualificationError, match="threshold"):
        _validated(budget, (forged,))


def test_dataclasses_replace_cannot_mint_pass_with_forged_checks() -> None:
    budget = PerformanceBudget(min_history_events=100)
    genuine = _validated(
        budget,
        (_check("history_events", ">=", 150, 100),),
    )
    forged = _check("caller_pass", ">=", 1, 1)

    with pytest.raises(PerformanceQualificationError, match="qualify_endurance_report"):
        replace(genuine, checks=(forged,))
