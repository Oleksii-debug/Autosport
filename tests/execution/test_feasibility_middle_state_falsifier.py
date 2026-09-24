from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from autosport.execution.feasibility import (
    ExecutionFeasibilityRequest,
    FeasibilityState,
    MarketBookSnapshot,
    PriceSize,
    ProjectionKind,
    ProviderLimitAuthority,
    SourceMode,
    _assess_execution_feasibility,
)


NOW = datetime(2026, 9, 21, 16, 30, tzinfo=timezone.utc)


def _request() -> ExecutionFeasibilityRequest:
    return ExecutionFeasibilityRequest(
        opportunity_id="opp-middle-state",
        opportunity_digest="opp-digest-middle-state",
        plan_id="plan-middle-state",
        plan_digest="plan-digest-middle-state",
        action_id="action-middle-state",
        action_digest="action-digest-middle-state",
        provider_id="betfair",
        account_id="acct-1",
        market_id="1.234",
        selection_id="42",
        requested_stake=Decimal("12"),
        limit_price=Decimal("2.00"),
        decision_at=NOW,
        expected_market_version=17,
        expected_inplay=False,
        expected_bet_delay_seconds=0,
    )


def _snapshot() -> MarketBookSnapshot:
    return MarketBookSnapshot(
        snapshot_id="book-middle-state",
        snapshot_digest="book-digest-middle-state",
        provider_id="betfair",
        account_id="acct-1",
        market_id="1.234",
        selection_id="42",
        source_mode=SourceMode.LIVE,
        projection_kind=ProjectionKind.EX_ALL_OFFERS,
        projection_depth=None,
        rollup_model=None,
        virtualise=False,
        is_truncated=False,
        status="OPEN",
        selection_status="ACTIVE",
        market_version=17,
        inplay=False,
        bet_delay_seconds=0,
        observed_at=NOW - timedelta(milliseconds=150),
        received_at=NOW - timedelta(milliseconds=100),
        sequence=None,
        has_ordering_gap=None,
        available_to_back=(
            PriceSize(Decimal("2.10"), Decimal("3")),
            PriceSize(Decimal("2.00"), Decimal("2")),
            PriceSize(Decimal("1.99"), Decimal("100")),
        ),
    )


def _limits() -> ProviderLimitAuthority:
    return ProviderLimitAuthority(
        provider_id="betfair",
        account_id="acct-1",
        market_id="1.234",
        evidence_digest="verified-limit-evidence",
        permitted=True,
        min_stake=Decimal("2"),
        max_stake=Decimal("1000"),
        min_price=Decimal("1.01"),
        max_price=Decimal("1000"),
    )


def _assess(book: MarketBookSnapshot):
    return _assess_execution_feasibility(
        _request(),
        book,
        _limits(),
        max_snapshot_age=timedelta(seconds=1),
        product_owned=True,
    )


def test_product_owned_measured_insufficient_depth_reaches_middle_state() -> None:
    result = _assess(_snapshot())

    assert result.displayed_acceptable_depth == Decimal("5")
    assert result.state is FeasibilityState.DISPLAYED_DEPTH_AT_SNAPSHOT
    assert result.sufficient is False
    assert result.reasons == ("DISPLAYED_DEPTH_INSUFFICIENT",)


def test_middle_state_never_masks_independent_freshness_failure() -> None:
    result = _assess(
        replace(
            _snapshot(),
            received_at=NOW + timedelta(milliseconds=1),
        )
    )

    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert result.sufficient is False
    assert "RECEIVED_AFTER_DECISION" in result.reasons
    assert "DISPLAYED_DEPTH_INSUFFICIENT" in result.reasons
