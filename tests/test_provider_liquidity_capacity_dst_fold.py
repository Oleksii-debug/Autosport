from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal

from autosport.provider_liquidity_capacity import (
    LiquidityEvidenceStatus,
    LiquidityLevel,
    LiquiditySide,
    LiquiditySnapshot,
    OfferProjection,
    ProjectionIdentity,
    assess_liquidity_capacity,
)


class _RepeatedHourTimezone(tzinfo):
    """Deterministic fold-sensitive zone without relying on host tzdata."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is not None and dt.fold == 1:
            return timedelta(hours=-5)
        return timedelta(hours=-4)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "TEST-FOLD"


def test_liquidity_freshness_uses_elapsed_instant_across_repeated_local_hour() -> None:
    zone = _RepeatedHourTimezone()
    captured_at = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    as_of = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)

    # The wall clock repeats 01:30, but the second occurrence is one real hour later.
    assert as_of - captured_at == timedelta(0)
    assert (
        as_of.astimezone(timezone.utc)
        - captured_at.astimezone(timezone.utc)
        == timedelta(hours=1)
    )

    snapshot = LiquiditySnapshot(
        provider="betfair",
        market_id="1.234",
        selection_id="77",
        currency="EUR",
        side=LiquiditySide.BACK,
        captured_at=captured_at,
        market_status="OPEN",
        runner_status="ACTIVE",
        projection=ProjectionIdentity(
            projection=OfferProjection.EX_ALL_OFFERS,
            virtualise=False,
            rollup_settings=(("model", "STAKE"),),
            depth=None,
        ),
        levels=(LiquidityLevel(Decimal("2.0"), Decimal("100")),),
    )

    assessment = assess_liquidity_capacity(
        snapshot,
        requested_size=Decimal("1"),
        requested_currency="EUR",
        limit_price=Decimal("2.0"),
        as_of=as_of,
        max_age=timedelta(minutes=30),
    )

    assert assessment.status is LiquidityEvidenceStatus.STALE_EVIDENCE
    assert assessment.supports_requested_size is False
    assert assessment.execution_guaranteed is False
