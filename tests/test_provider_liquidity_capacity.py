from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal

import pytest

from autosport.provider_liquidity_capacity import (
    LiquidityEvidenceStatus,
    LiquidityLevel,
    LiquiditySide,
    LiquiditySnapshot,
    OfferProjection,
    ProjectionIdentity,
    assess_liquidity_capacity,
)


NOW = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)


def _projection(
    projection: OfferProjection = OfferProjection.EX_BEST_OFFERS,
    *,
    depth: int | None = 3,
    virtualise: bool = False,
    rollover_stakes: bool = False,
    rollup_settings: tuple[tuple[str, str], ...] = (("model", "STAKE"),),
) -> ProjectionIdentity:
    if projection is OfferProjection.EX_ALL_OFFERS:
        depth = None
    return ProjectionIdentity(
        projection=projection,
        virtualise=virtualise,
        rollover_stakes=rollover_stakes,
        rollup_settings=rollup_settings,
        depth=depth,
    )


def _snapshot(
    *,
    side: LiquiditySide = LiquiditySide.BACK,
    captured_at: datetime = NOW,
    market_status: str = "OPEN",
    runner_status: str = "ACTIVE",
    projection: ProjectionIdentity | None = None,
    levels: tuple[LiquidityLevel, ...] = (),
) -> LiquiditySnapshot:
    return LiquiditySnapshot(
        provider="betfair",
        market_id="1.234",
        selection_id="77",
        currency="EUR",
        side=side,
        captured_at=captured_at,
        market_status=market_status,
        runner_status=runner_status,
        projection=projection or _projection(),
        levels=levels,
    )


def _assess(snapshot: LiquiditySnapshot, **overrides):
    kwargs = {
        "requested_size": Decimal("7"),
        "requested_currency": "EUR",
        "limit_price": Decimal("2.0"),
        "as_of": NOW,
        "max_age": timedelta(seconds=5),
    }
    kwargs.update(overrides)
    return assess_liquidity_capacity(snapshot, **kwargs)


def test_back_exact_visible_capacity_boundary_is_sufficient_but_not_guaranteed() -> None:
    result = _assess(
        _snapshot(
            levels=(
                LiquidityLevel(Decimal("2.0"), Decimal("3")),
                LiquidityLevel(Decimal("2.1"), Decimal("4")),
                LiquidityLevel(Decimal("1.99"), Decimal("100")),
            )
        )
    )

    assert result.status is LiquidityEvidenceStatus.SUFFICIENT_VISIBLE_CAPACITY
    assert result.observed_qualifying_size == Decimal("7")
    assert result.visible_capacity_numerically_sufficient is True
    assert result.provider_snapshot_origin_proven is False
    assert result.observation_time_proven is False
    assert result.supports_requested_size is False
    assert result.execution_guaranteed is False


def test_caller_authored_huge_ladder_cannot_mint_authoritative_support() -> None:
    result = _assess(
        _snapshot(
            projection=_projection(OfferProjection.EX_ALL_OFFERS),
            levels=(LiquidityLevel(Decimal("2.0"), Decimal("1000000000")),),
        ),
        requested_size=Decimal("999999999"),
    )

    assert result.status is LiquidityEvidenceStatus.SUFFICIENT_VISIBLE_CAPACITY
    assert result.observed_qualifying_size == Decimal("1000000000")
    assert result.visible_capacity_numerically_sufficient is True
    assert result.provider_snapshot_origin_proven is False
    assert result.observation_time_proven is False
    assert result.supports_requested_size is False
    assert result.execution_guaranteed is False


def test_best_offers_shortfall_is_indeterminate_not_zero_liquidity_claim() -> None:
    result = _assess(
        _snapshot(
            levels=(LiquidityLevel(Decimal("2.2"), Decimal("6")),),
        )
    )

    assert result.status is LiquidityEvidenceStatus.INDETERMINATE_TRUNCATED_BOOK
    assert result.observed_qualifying_size == Decimal("6")
    assert result.supports_requested_size is False
    assert "deeper liquidity" in result.reason


def test_lay_uses_maximum_acceptable_price_semantics() -> None:
    result = _assess(
        _snapshot(
            side=LiquiditySide.LAY,
            levels=(
                LiquidityLevel(Decimal("1.9"), Decimal("4")),
                LiquidityLevel(Decimal("2.0"), Decimal("3")),
                LiquidityLevel(Decimal("2.1"), Decimal("100")),
            ),
        )
    )

    assert result.status is LiquidityEvidenceStatus.SUFFICIENT_VISIBLE_CAPACITY
    assert result.observed_qualifying_size == Decimal("7")


def test_all_offers_shortfall_reports_observed_shortfall_only() -> None:
    result = _assess(
        _snapshot(
            projection=_projection(OfferProjection.EX_ALL_OFFERS),
            levels=(LiquidityLevel(Decimal("2.2"), Decimal("6")),),
        )
    )

    assert result.status is LiquidityEvidenceStatus.VISIBLE_CAPACITY_BELOW_REQUEST
    assert result.observed_qualifying_size == Decimal("6")
    assert result.execution_guaranteed is False
    assert "this observation" in result.reason


def test_currency_identity_is_bound_to_capacity_comparison() -> None:
    snapshot = _snapshot(
        levels=(LiquidityLevel(Decimal("2.0"), Decimal("100")),),
    )

    mismatch = _assess(snapshot, requested_currency="GBP")
    matching_casefold = _assess(snapshot, requested_currency="eur")

    assert mismatch.status is LiquidityEvidenceStatus.CURRENCY_MISMATCH
    assert mismatch.observed_qualifying_size == Decimal("0")
    assert matching_casefold.status is LiquidityEvidenceStatus.SUFFICIENT_VISIBLE_CAPACITY


def test_exact_max_age_boundary_is_usable_but_one_microsecond_older_is_stale() -> None:
    snapshot = _snapshot(
        captured_at=NOW - timedelta(seconds=5),
        levels=(LiquidityLevel(Decimal("2.0"), Decimal("7")),),
    )
    assert _assess(snapshot).status is LiquidityEvidenceStatus.SUFFICIENT_VISIBLE_CAPACITY

    stale = _snapshot(
        captured_at=NOW - timedelta(seconds=5, microseconds=1),
        levels=(LiquidityLevel(Decimal("2.0"), Decimal("7")),),
    )
    assert _assess(stale).status is LiquidityEvidenceStatus.STALE_EVIDENCE


def test_future_snapshot_fails_closed() -> None:
    result = _assess(
        _snapshot(
            captured_at=NOW + timedelta(microseconds=1),
            levels=(LiquidityLevel(Decimal("2.0"), Decimal("100")),),
        )
    )
    assert result.status is LiquidityEvidenceStatus.FUTURE_EVIDENCE
    assert result.observed_qualifying_size == Decimal("0")


@pytest.mark.parametrize(
    ("market_status", "runner_status"),
    [("CLOSED", "ACTIVE"), ("SUSPENDED", "ACTIVE"), ("OPEN", "REMOVED")],
)
def test_non_executable_market_or_runner_state_fails_closed(market_status: str, runner_status: str) -> None:
    result = _assess(
        _snapshot(
            market_status=market_status,
            runner_status=runner_status,
            levels=(LiquidityLevel(Decimal("2.0"), Decimal("100")),),
        )
    )
    assert result.status is LiquidityEvidenceStatus.UNUSABLE_MARKET_STATE
    assert result.observed_qualifying_size == Decimal("0")


def test_projection_identity_binds_depth_rollup_and_virtualise() -> None:
    base = _projection(depth=3, rollup_settings=(("limit", "10"), ("model", "STAKE")))
    same_reordered = _projection(depth=3, rollup_settings=(("model", "STAKE"), ("limit", "10")))
    different_depth = _projection(depth=5, rollup_settings=(("model", "STAKE"), ("limit", "10")))
    different_rollup = _projection(depth=3, rollup_settings=(("model", "PAYOUT"), ("limit", "10")))
    different_virtualise = _projection(
        depth=3,
        rollup_settings=(("model", "STAKE"), ("limit", "10")),
        virtualise=True,
    )

    assert base == same_reordered
    assert base != different_depth
    assert base != different_rollup
    assert base != different_virtualise


def test_ladder_order_does_not_change_capacity() -> None:
    levels = (
        LiquidityLevel(Decimal("2.2"), Decimal("4")),
        LiquidityLevel(Decimal("2.0"), Decimal("3")),
        LiquidityLevel(Decimal("1.9"), Decimal("50")),
    )
    forward = _assess(_snapshot(levels=levels))
    reverse = _assess(_snapshot(levels=tuple(reversed(levels))))
    assert forward.observed_qualifying_size == reverse.observed_qualifying_size == Decimal("7")
    assert forward.status is reverse.status is LiquidityEvidenceStatus.SUFFICIENT_VISIBLE_CAPACITY


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_level_values_are_rejected(bad: Decimal) -> None:
    with pytest.raises(ValueError):
        LiquidityLevel(price=bad, size=Decimal("1"))
    with pytest.raises(ValueError):
        LiquidityLevel(price=Decimal("2"), size=bad)


@pytest.mark.parametrize("field", ["requested_size", "limit_price"])
@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_assessment_decimals_are_rejected(field: str, bad: Decimal) -> None:
    with pytest.raises(ValueError):
        _assess(_snapshot(), **{field: bad})


def test_naive_timestamps_and_negative_max_age_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _snapshot(captured_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        _assess(_snapshot(), as_of=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="must not be negative"):
        _assess(_snapshot(), max_age=timedelta(microseconds=-1))


def test_duplicate_prices_are_rejected_to_prevent_double_counting() -> None:
    with pytest.raises(ValueError, match="duplicate prices"):
        _snapshot(
            levels=(
                LiquidityLevel(Decimal("2.0"), Decimal("1")),
                LiquidityLevel(Decimal("2.0"), Decimal("2")),
            )
        )


def test_snapshot_rejects_more_levels_than_declared_best_offers_depth() -> None:
    with pytest.raises(ValueError, match="exceed declared"):
        _snapshot(
            projection=_projection(depth=2),
            levels=(
                LiquidityLevel(Decimal("2.0"), Decimal("1")),
                LiquidityLevel(Decimal("2.1"), Decimal("1")),
                LiquidityLevel(Decimal("2.2"), Decimal("1")),
            ),
        )


def test_projection_rejects_ambiguous_depth_and_duplicate_rollup_keys() -> None:
    with pytest.raises(ValueError, match="requires a positive integer depth"):
        ProjectionIdentity(
            OfferProjection.EX_BEST_OFFERS,
            virtualise=False,
            rollover_stakes=False,
            rollup_settings=(),
            depth=None,
        )
    with pytest.raises(ValueError, match="must not declare a bounded depth"):
        ProjectionIdentity(
            OfferProjection.EX_ALL_OFFERS,
            virtualise=False,
            rollover_stakes=False,
            rollup_settings=(),
            depth=3,
        )
    with pytest.raises(ValueError, match="duplicate rollup setting key"):
        ProjectionIdentity(
            OfferProjection.EX_BEST_OFFERS,
            virtualise=False,
            rollover_stakes=False,
            rollup_settings=(("model", "STAKE"), ("model", "PAYOUT")),
            depth=3,
        )


def test_projection_rejects_unknown_virtualise_semantics() -> None:
    with pytest.raises(TypeError, match="virtualise must be bool"):
        ProjectionIdentity(
            OfferProjection.EX_BEST_OFFERS,
            virtualise=None,  # type: ignore[arg-type]
            rollover_stakes=False,
            rollup_settings=(),
            depth=3,
        )


def test_projection_rejects_rollover_or_unknown_volume_semantics() -> None:
    with pytest.raises(TypeError, match="rollover_stakes must be bool"):
        ProjectionIdentity(
            OfferProjection.EX_BEST_OFFERS,
            virtualise=False,
            rollover_stakes=None,  # type: ignore[arg-type]
            rollup_settings=(),
            depth=3,
        )

    with pytest.raises(ValueError, match="rollover_stakes=true is unsupported"):
        ProjectionIdentity(
            OfferProjection.EX_BEST_OFFERS,
            virtualise=False,
            rollover_stakes=True,
            rollup_settings=(),
            depth=3,
        )

    explicit_independent_sizes = ProjectionIdentity(
        OfferProjection.EX_BEST_OFFERS,
        virtualise=False,
        rollover_stakes=False,
        rollup_settings=(),
        depth=3,
    )
    assert explicit_independent_sizes.rollover_stakes is False


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

    assert as_of - captured_at == timedelta(0)
    assert (
        as_of.astimezone(timezone.utc)
        - captured_at.astimezone(timezone.utc)
        == timedelta(hours=1)
    )

    result = assess_liquidity_capacity(
        _snapshot(
            captured_at=captured_at,
            projection=_projection(OfferProjection.EX_ALL_OFFERS),
            levels=(LiquidityLevel(Decimal("2.0"), Decimal("100")),),
        ),
        requested_size=Decimal("1"),
        requested_currency="EUR",
        limit_price=Decimal("2.0"),
        as_of=as_of,
        max_age=timedelta(minutes=30),
    )

    assert result.status is LiquidityEvidenceStatus.STALE_EVIDENCE
    assert result.snapshot_age == timedelta(hours=1)
    assert result.supports_requested_size is False
    assert result.execution_guaranteed is False
