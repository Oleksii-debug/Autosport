import pytest

from autosport.performance_qualification import (
    MetricQualification,
    PerformanceBudget,
    PerformanceQualification,
    PerformanceQualificationError,
    _validate_check_roster,
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


def test_no_caller_reachable_validated_factory_remains() -> None:
    assert not hasattr(PerformanceQualification, "_from_validated")


def test_roster_validator_rejects_unconstrained_pass_metric() -> None:
    budget = PerformanceBudget(min_history_events=100)
    forged = _check("caller_pass", ">=", 1, 1)

    with pytest.raises(PerformanceQualificationError, match="unconstrained"):
        _validate_check_roster(budget, (forged,))


def test_roster_validator_rejects_omitted_budget_check() -> None:
    budget = PerformanceBudget(
        min_history_events=100,
        max_peak_traced_memory_bytes=1024,
    )
    history = _check("history_events", ">=", 150, 100)

    with pytest.raises(PerformanceQualificationError, match="exactly cover"):
        _validate_check_roster(budget, (history,))


def test_roster_validator_rejects_duplicate_metric() -> None:
    budget = PerformanceBudget(min_history_events=100)
    history = _check("history_events", ">=", 150, 100)

    with pytest.raises(PerformanceQualificationError, match="duplicate"):
        _validate_check_roster(budget, (history, history))


def test_roster_validator_rejects_wrong_comparator() -> None:
    budget = PerformanceBudget(min_history_events=100)
    forged = _check("history_events", "<=", 50, 100)

    with pytest.raises(PerformanceQualificationError, match="comparator"):
        _validate_check_roster(budget, (forged,))


def test_roster_validator_rejects_wrong_threshold() -> None:
    budget = PerformanceBudget(min_history_events=100)
    forged = _check("history_events", ">=", 150, 1)

    with pytest.raises(PerformanceQualificationError, match="threshold"):
        _validate_check_roster(budget, (forged,))
