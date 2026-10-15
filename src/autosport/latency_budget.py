"""Deterministic latency-budget measurement primitives.

This module measures elapsed time against explicit budgets.  It provides
instrumentation only: synthetic or local measurements do not constitute
release, execution, readiness, or real-hardware performance evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Final, TypeVar


SCHEMA: Final = "autosport.latency_budget"
SCHEMA_VERSION: Final = 1

# Bound aggregation of untrusted or long-lived latency sample streams.
# Larger campaigns must aggregate in bounded windows, not exhaust an infinite source.
MAX_LATENCY_SAMPLES: Final = 100_000

_T = TypeVar("_T")


class LatencyBudgetError(ValueError):
    """Raised when a latency budget or measurement is not canonical."""


def _canonical_label(value: str, *, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise LatencyBudgetError(f"{field} must be a non-empty canonical string")
    return value


def _non_negative_ns(value: int, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise LatencyBudgetError(f"{field} must be a non-negative integer nanosecond value")
    return value


@dataclass(frozen=True, slots=True)
class LatencyBudget:
    """An explicit positive elapsed-time budget in integer nanoseconds."""

    name: str
    budget_ns: int

    def __post_init__(self) -> None:
        _canonical_label(self.name, field="name")
        if (
            type(self.budget_ns) is not int
            or self.budget_ns <= 0
        ):
            raise LatencyBudgetError("budget_ns must be a positive integer nanosecond value")

    @classmethod
    def from_milliseconds(cls, name: str, milliseconds: int) -> LatencyBudget:
        """Build a budget from an exact positive integer millisecond value."""

        if (
            type(milliseconds) is not int
            or milliseconds <= 0
        ):
            raise LatencyBudgetError("milliseconds must be a positive integer value")
        return cls(name=name, budget_ns=milliseconds * 1_000_000)

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "budget_ns": self.budget_ns}


@dataclass(frozen=True, slots=True)
class LatencyMeasurement:
    """One successful-call elapsed-time observation against a budget."""

    operation: str
    elapsed_ns: int
    budget: LatencyBudget

    def __post_init__(self) -> None:
        _canonical_label(self.operation, field="operation")
        _non_negative_ns(self.elapsed_ns, field="elapsed_ns")
        if type(self.budget) is not LatencyBudget:
            raise LatencyBudgetError("budget must be an exact LatencyBudget")

    @property
    def within_budget(self) -> bool:
        return self.elapsed_ns <= self.budget.budget_ns

    @property
    def overrun_ns(self) -> int:
        return max(0, self.elapsed_ns - self.budget.budget_ns)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "operation": self.operation,
            "elapsed_ns": self.elapsed_ns,
            "budget": self.budget.to_dict(),
            "within_budget": self.within_budget,
            "overrun_ns": self.overrun_ns,
            "release_authority": False,
            "execution_authority": False,
            "readiness_authority": False,
            "real_hardware_performance_evidence": False,
            "whole_product_complete": False,
        }


@dataclass(frozen=True, slots=True)
class LatencySummary:
    """Deterministic nearest-rank summary of elapsed-time samples."""

    budget: LatencyBudget
    sample_count: int
    total_ns: int
    min_ns: int
    p50_ns: int
    p95_ns: int
    p99_ns: int
    max_ns: int
    breach_count: int

    def __post_init__(self) -> None:
        if type(self.budget) is not LatencyBudget:
            raise LatencyBudgetError("budget must be an exact LatencyBudget")
        integer_fields = {
            "sample_count": self.sample_count,
            "total_ns": self.total_ns,
            "min_ns": self.min_ns,
            "p50_ns": self.p50_ns,
            "p95_ns": self.p95_ns,
            "p99_ns": self.p99_ns,
            "max_ns": self.max_ns,
            "breach_count": self.breach_count,
        }
        if (
            type(self.sample_count) is not int
            or self.sample_count <= 0
        ):
            raise LatencyBudgetError("sample_count must be a positive integer")
        for field, value in integer_fields.items():
            if field == "sample_count":
                continue
            _non_negative_ns(value, field=field)
        if self.breach_count > self.sample_count:
            raise LatencyBudgetError("breach_count cannot exceed sample_count")
        if self.max_ns <= self.budget.budget_ns and self.breach_count != 0:
            raise LatencyBudgetError("breach_count contradicts all-within-budget samples")
        if self.max_ns > self.budget.budget_ns and self.breach_count == 0:
            raise LatencyBudgetError("breach_count contradicts max_ns budget breach")
        if self.min_ns > self.budget.budget_ns and self.breach_count != self.sample_count:
            raise LatencyBudgetError("breach_count contradicts all-over-budget samples")
        if not (
            self.min_ns
            <= self.p50_ns
            <= self.p95_ns
            <= self.p99_ns
            <= self.max_ns
        ):
            raise LatencyBudgetError("latency summary ranks must be monotonic")
        if self.sample_count == 1:
            if not (
                self.min_ns
                == self.p50_ns
                == self.p95_ns
                == self.p99_ns
                == self.max_ns
                == self.total_ns
            ):
                raise LatencyBudgetError(
                    "single-sample summary fields must describe one exact sample"
                )
        else:
            # Any realizable sample set containing both extrema must include at least
            # one min and one max. The remaining samples can only lie between them.
            minimum_total = (
                self.max_ns + (self.sample_count - 1) * self.min_ns
            )
            maximum_total = (
                self.min_ns + (self.sample_count - 1) * self.max_ns
            )
            if not minimum_total <= self.total_ns <= maximum_total:
                raise LatencyBudgetError(
                    "total_ns contradicts realizable sample_count/min_ns/max_ns bounds"
                )

    @property
    def within_budget(self) -> bool:
        return self.breach_count == 0

    @property
    def worst_overrun_ns(self) -> int:
        return max(0, self.max_ns - self.budget.budget_ns)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "budget": self.budget.to_dict(),
            "sample_count": self.sample_count,
            "total_ns": self.total_ns,
            "min_ns": self.min_ns,
            "p50_ns": self.p50_ns,
            "p95_ns": self.p95_ns,
            "p99_ns": self.p99_ns,
            "max_ns": self.max_ns,
            "breach_count": self.breach_count,
            "within_budget": self.within_budget,
            "worst_overrun_ns": self.worst_overrun_ns,
            "percentile_method": "nearest-rank",
            "release_authority": False,
            "execution_authority": False,
            "readiness_authority": False,
            "real_hardware_performance_evidence": False,
            "whole_product_complete": False,
        }


def _nearest_rank(sorted_samples: tuple[int, ...], percentile: int) -> int:
    rank = (percentile * len(sorted_samples) + 99) // 100
    return sorted_samples[rank - 1]


def summarize_latency(samples_ns: Iterable[int], *, budget: LatencyBudget) -> LatencySummary:
    """Summarize samples using deterministic integer nearest-rank percentiles."""

    if type(budget) is not LatencyBudget:
        raise LatencyBudgetError("budget must be an exact LatencyBudget")

    validated: list[int] = []
    for index, value in enumerate(samples_ns):
        if index >= MAX_LATENCY_SAMPLES:
            raise LatencyBudgetError(
                f"samples_ns exceeds maximum {MAX_LATENCY_SAMPLES} samples"
            )
        validated.append(_non_negative_ns(value, field=f"samples_ns[{index}]"))
    if not validated:
        raise LatencyBudgetError("samples_ns must contain at least one sample")

    ordered = tuple(sorted(validated))
    budget_ns = budget.budget_ns
    return LatencySummary(
        budget=budget,
        sample_count=len(ordered),
        total_ns=sum(ordered),
        min_ns=ordered[0],
        p50_ns=_nearest_rank(ordered, 50),
        p95_ns=_nearest_rank(ordered, 95),
        p99_ns=_nearest_rank(ordered, 99),
        max_ns=ordered[-1],
        breach_count=sum(value > budget_ns for value in ordered),
    )


def measure_call(
    operation: str,
    call: Callable[[], _T],
    *,
    budget: LatencyBudget,
    clock_ns: Callable[[], int] = perf_counter_ns,
) -> tuple[_T, LatencyMeasurement]:
    """Measure one successful call using an injectable monotonic-ns clock.

    The wrapped call's exception is propagated unchanged.  A clock that moves
    backwards fails closed rather than minting a zero or negative duration.
    """

    _canonical_label(operation, field="operation")
    if type(budget) is not LatencyBudget:
        raise LatencyBudgetError("budget must be an exact LatencyBudget")
    if not callable(call):
        raise LatencyBudgetError("call must be callable")
    if not callable(clock_ns):
        raise LatencyBudgetError("clock_ns must be callable")

    start_ns = clock_ns()
    if (
        type(start_ns) is not int
        or start_ns < 0
    ):
        raise LatencyBudgetError(
            "clock_ns must return non-negative integer nanoseconds"
        )
    result = call()
    end_ns = clock_ns()
    if (
        type(end_ns) is not int
        or end_ns < 0
    ):
        raise LatencyBudgetError(
            "clock_ns must return non-negative integer nanoseconds"
        )
    if end_ns < start_ns:
        raise LatencyBudgetError("clock_ns moved backwards")

    return result, LatencyMeasurement(
        operation=operation,
        elapsed_ns=end_ns - start_ns,
        budget=budget,
    )
