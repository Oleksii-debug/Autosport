from __future__ import annotations

"""Causal feature-engineering primitives for Autosport research.

First-party lineage
-------------------
This module adapts concepts already implemented in the owner's Nika-Core
``src/nika_core/trading_research/causality.py`` and its adversarial tests.
The Autosport version is deliberately domain-neutral at the feature layer and
keeps Autosport's existing dataset/replay/evidence authorities unchanged.

Donor checkpoint reviewed during adoption:
- repository: Oleksii-debug/Nika-Core
- main commit: 2f7be3389109d7dd6fb3bae40540fe0cf2eba695
- donor files: trading_research/causality.py and
  tests/test_trading_research_causality.py

No Nika runtime, scheduler, permission, persistence, or domain authority is
imported here.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


class CausalFeatureError(ValueError):
    """Base error for fail-closed feature research operations."""


class FeatureLeakageError(CausalFeatureError):
    """Raised when a feature operation could expose future information."""


class DatasetPartition(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


def require_aware_utc(value: datetime, field_name: str) -> datetime:
    """Normalize one aware timestamp to UTC and reject naive timestamps."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise CausalFeatureError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class FeaturePoint:
    """One feature value together with the time it became knowable."""

    value: Decimal | None
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "available_at",
            require_aware_utc(self.available_at, "available_at"),
        )


@dataclass(frozen=True, slots=True)
class FeatureLineage:
    """Availability lineage for a derived feature.

    A derived feature cannot claim to exist before its latest input became
    available. This is separate from event/source time and protects causal
    walk-forward research from accidental information leakage.
    """

    name: str
    input_names: tuple[str, ...]
    input_available_at: tuple[datetime, ...]
    available_at: datetime

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise CausalFeatureError("feature lineage name must not be empty")
        if len(self.input_names) != len(self.input_available_at):
            raise CausalFeatureError(
                "feature lineage input_names and input_available_at must have equal length"
            )
        if any(not name.strip() for name in self.input_names):
            raise CausalFeatureError("feature lineage input names must not be empty")

        inputs = tuple(
            require_aware_utc(value, "input_available_at")
            for value in self.input_available_at
        )
        available_at = require_aware_utc(self.available_at, "available_at")
        if inputs and available_at < max(inputs):
            raise FeatureLeakageError(
                "derived feature cannot be available before its latest input"
            )
        object.__setattr__(self, "input_available_at", inputs)
        object.__setattr__(self, "available_at", available_at)


def causal_shift(
    points: Sequence[FeaturePoint], periods: int
) -> tuple[FeaturePoint, ...]:
    """Lag a series without permitting negative/future shifts."""

    if periods < 0:
        raise FeatureLeakageError("negative shift would expose future values")
    if periods == 0:
        return tuple(points)

    result: list[FeaturePoint] = []
    for index, point in enumerate(points):
        if index < periods:
            result.append(FeaturePoint(None, point.available_at))
        else:
            source = points[index - periods]
            result.append(FeaturePoint(source.value, point.available_at))
    return tuple(result)


def trailing_mean(
    points: Sequence[FeaturePoint],
    window: int,
    *,
    centered: bool = False,
) -> tuple[FeaturePoint, ...]:
    """Compute a trailing Decimal mean using only current/past values."""

    if centered:
        raise FeatureLeakageError("centered rolling windows use future observations")
    if window <= 0:
        raise CausalFeatureError("window must be positive")

    result: list[FeaturePoint] = []
    for index, point in enumerate(points):
        start = max(0, index - window + 1)
        values = [
            candidate.value
            for candidate in points[start : index + 1]
            if candidate.value is not None
        ]
        mean = (
            None
            if not values
            else sum(values, Decimal(0)) / Decimal(len(values))
        )
        result.append(FeaturePoint(mean, point.available_at))
    return tuple(result)


def fill_missing(
    points: Sequence[FeaturePoint],
    *,
    method: str = "forward",
) -> tuple[FeaturePoint, ...]:
    """Fill missing values only with information already known at that point."""

    if method != "forward":
        raise FeatureLeakageError(
            "only forward fill is causal; backward/non-causal fill is forbidden"
        )

    last: Decimal | None = None
    result: list[FeaturePoint] = []
    for point in points:
        if point.value is not None:
            last = point.value
        result.append(FeaturePoint(last, point.available_at))
    return tuple(result)


class TrainOnlyStandardizer:
    """Simple Decimal standardizer whose fit is restricted to TRAIN data."""

    __slots__ = ("_fitted", "_mean", "_scale")

    def __init__(self) -> None:
        self._mean = Decimal(0)
        self._scale = Decimal(1)
        self._fitted = False

    @property
    def fitted(self) -> bool:
        return self._fitted

    def fit(
        self,
        values: Iterable[Decimal],
        *,
        partition: DatasetPartition,
    ) -> None:
        if partition is not DatasetPartition.TRAIN:
            raise FeatureLeakageError(
                "feature/scaler fitting is allowed only on the train partition"
            )
        materialized = tuple(Decimal(value) for value in values)
        if not materialized:
            raise CausalFeatureError("cannot fit empty values")

        mean = sum(materialized, Decimal(0)) / Decimal(len(materialized))
        variance = (
            sum((value - mean) ** 2 for value in materialized)
            / Decimal(len(materialized))
        )
        self._mean = mean
        self._scale = variance.sqrt() if variance > 0 else Decimal(1)
        self._fitted = True

    def transform(self, values: Iterable[Decimal]) -> tuple[Decimal, ...]:
        if not self._fitted:
            raise FeatureLeakageError(
                "standardizer must be fit on train data before transform"
            )
        return tuple(
            (Decimal(value) - self._mean) / self._scale
            for value in values
        )


class AvailabilityCache:
    """In-memory cache that enforces decision-time visibility on read."""

    __slots__ = ("_values",)

    def __init__(self) -> None:
        self._values: dict[str, tuple[datetime, object]] = {}

    def put(self, key: str, value: object, *, available_at: datetime) -> None:
        if not key.strip():
            raise CausalFeatureError("cache key must not be empty")
        self._values[key] = (
            require_aware_utc(available_at, "available_at"),
            value,
        )

    def get(self, key: str, *, at: datetime) -> object | None:
        decision_at = require_aware_utc(at, "at")
        stored = self._values.get(key)
        if stored is None:
            return None
        available_at, value = stored
        if available_at > decision_at:
            raise FeatureLeakageError(
                "future cache entry is not visible at decision time"
            )
        return value


__all__ = [
    "AvailabilityCache",
    "CausalFeatureError",
    "DatasetPartition",
    "FeatureLeakageError",
    "FeatureLineage",
    "FeaturePoint",
    "TrainOnlyStandardizer",
    "causal_shift",
    "fill_missing",
    "require_aware_utc",
    "trailing_mean",
]
