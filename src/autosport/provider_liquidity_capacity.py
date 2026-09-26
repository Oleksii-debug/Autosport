from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum


class LiquiditySide(str, Enum):
    BACK = "BACK"
    LAY = "LAY"


class OfferProjection(str, Enum):
    EX_BEST_OFFERS = "EX_BEST_OFFERS"
    EX_ALL_OFFERS = "EX_ALL_OFFERS"


class LiquidityEvidenceStatus(str, Enum):
    SUFFICIENT_VISIBLE_CAPACITY = "SUFFICIENT_VISIBLE_CAPACITY"
    VISIBLE_CAPACITY_BELOW_REQUEST = "VISIBLE_CAPACITY_BELOW_REQUEST"
    INDETERMINATE_TRUNCATED_BOOK = "INDETERMINATE_TRUNCATED_BOOK"
    STALE_EVIDENCE = "STALE_EVIDENCE"
    FUTURE_EVIDENCE = "FUTURE_EVIDENCE"
    UNUSABLE_MARKET_STATE = "UNUSABLE_MARKET_STATE"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"


def _require_positive_finite(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_non_empty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class LiquidityLevel:
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        _require_positive_finite(self.price, "price")
        _require_positive_finite(self.size, "size")


@dataclass(frozen=True)
class ProjectionIdentity:
    """Identity of provider settings that determine returned price-size evidence."""

    projection: OfferProjection
    virtualise: bool
    rollover_stakes: bool
    rollup_settings: tuple[tuple[str, str], ...]
    depth: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.projection, OfferProjection):
            raise TypeError("projection must be OfferProjection")
        if not isinstance(self.virtualise, bool):
            raise TypeError("virtualise must be bool")
        if not isinstance(self.rollover_stakes, bool):
            raise TypeError("rollover_stakes must be bool")
        if self.rollover_stakes:
            raise ValueError(
                "rollover_stakes=true is unsupported for independent level aggregation"
            )
        if self.projection is OfferProjection.EX_BEST_OFFERS:
            if not isinstance(self.depth, int) or isinstance(self.depth, bool) or self.depth <= 0:
                raise ValueError("EX_BEST_OFFERS requires a positive integer depth")
        elif self.depth is not None:
            raise ValueError("EX_ALL_OFFERS must not declare a bounded depth")

        canonical: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in self.rollup_settings:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("rollup_settings entries must be (key, value) tuples")
            key, value = item
            _require_non_empty(key, "rollup setting key")
            _require_non_empty(value, "rollup setting value")
            if key in seen:
                raise ValueError(f"duplicate rollup setting key: {key}")
            seen.add(key)
            canonical.append((key, value))
        object.__setattr__(self, "rollup_settings", tuple(sorted(canonical)))


@dataclass(frozen=True)
class LiquiditySnapshot:
    provider: str
    market_id: str
    selection_id: str
    currency: str
    side: LiquiditySide
    captured_at: datetime
    market_status: str
    runner_status: str
    projection: ProjectionIdentity
    levels: tuple[LiquidityLevel, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.provider, "provider")
        _require_non_empty(self.market_id, "market_id")
        _require_non_empty(self.selection_id, "selection_id")
        _require_non_empty(self.currency, "currency")
        object.__setattr__(self, "currency", self.currency.strip().upper())
        if not isinstance(self.side, LiquiditySide):
            raise TypeError("side must be LiquiditySide")
        _require_aware(self.captured_at, "captured_at")
        _require_non_empty(self.market_status, "market_status")
        _require_non_empty(self.runner_status, "runner_status")
        if not isinstance(self.projection, ProjectionIdentity):
            raise TypeError("projection must be ProjectionIdentity")
        levels = tuple(self.levels)
        if not all(isinstance(level, LiquidityLevel) for level in levels):
            raise TypeError("levels must contain LiquidityLevel values")
        prices = [level.price for level in levels]
        if len(prices) != len(set(prices)):
            raise ValueError("levels must not contain duplicate prices")
        if (
            self.projection.projection is OfferProjection.EX_BEST_OFFERS
            and self.projection.depth is not None
            and len(levels) > self.projection.depth
        ):
            raise ValueError("levels exceed declared EX_BEST_OFFERS depth")
        object.__setattr__(self, "levels", levels)


@dataclass(frozen=True)
class LiquidityCapacityAssessment:
    status: LiquidityEvidenceStatus
    requested_size: Decimal
    requested_currency: str
    limit_price: Decimal
    observed_qualifying_size: Decimal
    snapshot_age: timedelta
    reason: str

    @property
    def visible_capacity_numerically_sufficient(self) -> bool:
        """Whether the supplied ladder arithmetic covers the requested size.

        This is descriptive arithmetic over caller-supplied observation data. It is
        not proof that the provider emitted the snapshot or that its timestamp is
        product-owned.
        """

        return self.status is LiquidityEvidenceStatus.SUFFICIENT_VISIBLE_CAPACITY

    @property
    def provider_snapshot_origin_proven(self) -> bool:
        """This caller-constructible contract cannot prove provider acquisition."""

        return False

    @property
    def observation_time_proven(self) -> bool:
        """This caller-constructible contract cannot prove provider observation time."""

        return False

    @property
    def supports_requested_size(self) -> bool:
        """Authoritative support requires independent provider-origin/time proof."""

        return (
            self.visible_capacity_numerically_sufficient
            and self.provider_snapshot_origin_proven
            and self.observation_time_proven
        )

    @property
    def execution_guaranteed(self) -> bool:
        """A provider book snapshot is observational evidence, never a fill guarantee."""

        return False


def assess_liquidity_capacity(
    snapshot: LiquiditySnapshot,
    *,
    requested_size: Decimal,
    requested_currency: str,
    limit_price: Decimal,
    as_of: datetime,
    max_age: timedelta,
) -> LiquidityCapacityAssessment:
    """Assess visible price-size capacity without claiming future execution.

    For BACK, prices at or above ``limit_price`` qualify. For LAY, prices at or
    below ``limit_price`` qualify. A bounded best-offers projection that does not
    show enough size is indeterminate rather than proof that deeper liquidity is
    absent.
    """

    if not isinstance(snapshot, LiquiditySnapshot):
        raise TypeError("snapshot must be LiquiditySnapshot")
    _require_positive_finite(requested_size, "requested_size")
    _require_non_empty(requested_currency, "requested_currency")
    requested_currency = requested_currency.strip().upper()
    _require_positive_finite(limit_price, "limit_price")
    _require_aware(as_of, "as_of")
    if not isinstance(max_age, timedelta):
        raise TypeError("max_age must be timedelta")
    if max_age < timedelta(0):
        raise ValueError("max_age must not be negative")

    # Aware datetime subtraction with the same tzinfo object intentionally uses
    # wall-clock arithmetic and can erase a repeated-hour DST fold. Freshness is
    # elapsed-time authority, so compare absolute UTC instants instead.
    age = (
        as_of.astimezone(timezone.utc)
        - snapshot.captured_at.astimezone(timezone.utc)
    )
    if age < timedelta(0):
        return LiquidityCapacityAssessment(
            status=LiquidityEvidenceStatus.FUTURE_EVIDENCE,
            requested_size=requested_size,
            requested_currency=requested_currency,
            limit_price=limit_price,
            observed_qualifying_size=Decimal("0"),
            snapshot_age=age,
            reason="snapshot timestamp is later than the assessment time",
        )
    if age > max_age:
        return LiquidityCapacityAssessment(
            status=LiquidityEvidenceStatus.STALE_EVIDENCE,
            requested_size=requested_size,
            requested_currency=requested_currency,
            limit_price=limit_price,
            observed_qualifying_size=Decimal("0"),
            snapshot_age=age,
            reason="snapshot is older than the allowed evidence age",
        )
    if snapshot.currency != requested_currency:
        return LiquidityCapacityAssessment(
            status=LiquidityEvidenceStatus.CURRENCY_MISMATCH,
            requested_size=requested_size,
            requested_currency=requested_currency,
            limit_price=limit_price,
            observed_qualifying_size=Decimal("0"),
            snapshot_age=age,
            reason="snapshot currency does not match requested stake currency",
        )
    if snapshot.market_status.upper() != "OPEN" or snapshot.runner_status.upper() != "ACTIVE":
        return LiquidityCapacityAssessment(
            status=LiquidityEvidenceStatus.UNUSABLE_MARKET_STATE,
            requested_size=requested_size,
            requested_currency=requested_currency,
            limit_price=limit_price,
            observed_qualifying_size=Decimal("0"),
            snapshot_age=age,
            reason="market must be OPEN and runner must be ACTIVE",
        )

    if snapshot.side is LiquiditySide.BACK:
        qualifying = (level for level in snapshot.levels if level.price >= limit_price)
    else:
        qualifying = (level for level in snapshot.levels if level.price <= limit_price)
    observed = sum((level.size for level in qualifying), Decimal("0"))

    if observed >= requested_size:
        status = LiquidityEvidenceStatus.SUFFICIENT_VISIBLE_CAPACITY
        reason = "snapshot shows enough visible size at acceptable prices"
    elif snapshot.projection.projection is OfferProjection.EX_BEST_OFFERS:
        status = LiquidityEvidenceStatus.INDETERMINATE_TRUNCATED_BOOK
        reason = "bounded best-offers snapshot does not show enough size; deeper liquidity was not observed"
    else:
        status = LiquidityEvidenceStatus.VISIBLE_CAPACITY_BELOW_REQUEST
        reason = "all-offers snapshot shows less qualifying size than requested at this observation"

    return LiquidityCapacityAssessment(
        status=status,
        requested_size=requested_size,
        requested_currency=requested_currency,
        limit_price=limit_price,
        observed_qualifying_size=observed,
        snapshot_age=age,
        reason=reason,
    )
