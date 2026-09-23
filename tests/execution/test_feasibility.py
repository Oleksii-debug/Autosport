from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.execution.feasibility import (
    ExecutionFeasibilityRequest,
    FeasibilityState,
    MarketBookSnapshot,
    PriceSize,
    ProjectionKind,
    ProviderLimitAuthority,
    SourceMode,
    assess_execution_feasibility,
)


NOW = datetime(2026, 9, 21, 6, 30, tzinfo=timezone.utc)


def request() -> ExecutionFeasibilityRequest:
    return ExecutionFeasibilityRequest(
        opportunity_id="opp-1",
        opportunity_digest="opp-digest",
        plan_id="plan-1",
        plan_digest="plan-digest",
        action_id="action-1",
        action_digest="action-digest",
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


def snapshot() -> MarketBookSnapshot:
    return MarketBookSnapshot(
        snapshot_id="book-17-99",
        snapshot_digest="book-digest",
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
        sequence=99,
        has_ordering_gap=False,
        available_to_back=(
            PriceSize(Decimal("2.10"), Decimal("5")),
            PriceSize(Decimal("2.04"), Decimal("4")),
            PriceSize(Decimal("2.00"), Decimal("6")),
            PriceSize(Decimal("1.99"), Decimal("100")),
        ),
    )


def limits() -> ProviderLimitAuthority:
    return ProviderLimitAuthority(
        provider_id="betfair",
        account_id="acct-1",
        market_id="1.234",
        evidence_digest="limits-digest",
        permitted=True,
        min_stake=Decimal("2"),
        max_stake=Decimal("1000"),
        min_price=Decimal("1.01"),
        max_price=Decimal("1000"),
    )


def assess(
    *,
    req: ExecutionFeasibilityRequest | None = None,
    book: MarketBookSnapshot | None = None,
    authority: ProviderLimitAuthority | None = None,
):
    return assess_execution_feasibility(
        req or request(),
        book or snapshot(),
        authority or limits(),
        max_snapshot_age=timedelta(seconds=1),
    )


def test_caller_minted_consistent_dtos_cannot_issue_positive_feasibility() -> None:
    result = assess()

    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert result.sufficient is False
    assert result.displayed_acceptable_depth == Decimal("15")
    assert result.reasons == ("PRODUCT_OWNED_EVIDENCE_UNRESOLVED",)
    assert len(result.evidence_digest) == 64
    assert len(result.liquidity_overlap_key) == 64


def test_worse_than_limit_price_is_not_counted() -> None:
    result = assess(req=replace(request(), requested_stake=Decimal("16")))

    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert result.displayed_acceptable_depth == Decimal("15")
    assert result.reasons == (
        "PRODUCT_OWNED_EVIDENCE_UNRESOLVED",
        "DISPLAYED_DEPTH_INSUFFICIENT",
    )


def test_back_limit_consumes_opposing_available_to_back_depth() -> None:
    book = replace(
        snapshot(),
        available_to_back=(
            PriceSize(Decimal("2.10"), Decimal("3")),
            PriceSize(Decimal("1.99"), Decimal("100")),
        ),
    )

    result = assess(book=book)

    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert result.displayed_acceptable_depth == Decimal("3")
    assert result.reasons == (
        "PRODUCT_OWNED_EVIDENCE_UNRESOLVED",
        "DISPLAYED_DEPTH_INSUFFICIENT",
    )


@pytest.mark.parametrize(
    ("book", "reason"),
    [
        (replace(snapshot(), source_mode=SourceMode.DELAYED), "DELAYED_SOURCE"),
        (replace(snapshot(), observed_at=NOW - timedelta(seconds=2)), "STALE_SNAPSHOT"),
        (replace(snapshot(), observed_at=NOW + timedelta(milliseconds=1)), "FUTURE_SNAPSHOT"),
        (replace(snapshot(), received_at=NOW + timedelta(milliseconds=1)), "RECEIVED_AFTER_DECISION"),
        (replace(snapshot(), has_ordering_gap=True), "SNAPSHOT_ORDERING_GAP"),
        (replace(snapshot(), status="SUSPENDED"), "MARKET_NOT_OPEN"),
        (replace(snapshot(), selection_status="REMOVED"), "SELECTION_NOT_ACTIVE"),
        (replace(snapshot(), is_truncated=True), "TRUNCATED_PROJECTION"),
        (replace(snapshot(), virtualise=True), "VIRTUALISED_LADDER_FORBIDDEN"),
        (replace(snapshot(), rollup_model="STAKE"), "ROLLUP_SUBSTITUTION_FORBIDDEN"),
        (replace(snapshot(), market_version=18), "MARKET_VERSION_MISMATCH"),
        (replace(snapshot(), inplay=True), "INPLAY_MISMATCH"),
        (replace(snapshot(), bet_delay_seconds=5), "BET_DELAY_MISMATCH"),
        (replace(snapshot(), account_id="other"), "ACCOUNT_ID_MISMATCH"),
    ],
)
def test_snapshot_uncertainty_fails_closed(book: MarketBookSnapshot, reason: str) -> None:
    result = assess(book=book)
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert result.sufficient is False
    assert reason in result.reasons


@pytest.mark.parametrize(
    ("req", "reason"),
    [
        (replace(request(), side="LAY"), "UNSUPPORTED_SIDE"),
        (replace(request(), order_type="MARKET"), "UNSUPPORTED_ORDER_TYPE"),
        (replace(request(), leg_count=2), "MULTI_LEG_LIQUIDITY_REUSE_FORBIDDEN"),
        (replace(request(), fill_or_kill=True), "FILL_OR_KILL_NOT_STANDARD_LIMIT"),
        (replace(request(), minimum_fill_size=Decimal("5")), "MINIMUM_FILL_NOT_STANDARD_LIMIT"),
        (replace(request(), bet_target_type="PAYOUT"), "BET_TARGET_FORBIDDEN"),
        (replace(request(), smart_order=True), "SMART_ORDER_FORBIDDEN"),
    ],
)
def test_nonstandard_or_reusable_order_semantics_fail_closed(req: ExecutionFeasibilityRequest, reason: str) -> None:
    result = assess(req=req)
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert reason in result.reasons


def test_best_offers_projection_must_bind_depth_and_not_claim_truncation() -> None:
    unbound = replace(snapshot(), projection_kind=ProjectionKind.EX_BEST_OFFERS, projection_depth=None)
    truncated = replace(snapshot(), projection_kind=ProjectionKind.EX_BEST_OFFERS, projection_depth=3, is_truncated=True)

    assert assess(book=unbound).state is FeasibilityState.UNKNOWN_UNPROVEN
    assert "BEST_OFFERS_DEPTH_UNBOUND" in assess(book=unbound).reasons
    assert assess(book=truncated).state is FeasibilityState.UNKNOWN_UNPROVEN
    assert "TRUNCATED_PROJECTION" in assess(book=truncated).reasons


def test_limit_authority_is_identity_bound_and_fail_closed() -> None:
    mismatched = replace(limits(), market_id="9.999")
    rejected = replace(limits(), permitted=False)

    result = assess(authority=mismatched)
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert "LIMIT_MARKET_MISMATCH" in result.reasons

    result = assess(authority=rejected)
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert "LIMIT_AUTHORITY_REJECTED" in result.reasons


def test_evidence_digest_changes_when_causal_evidence_changes() -> None:
    first = assess()
    second = assess(book=replace(snapshot(), sequence=100, snapshot_digest="book-digest-2"))
    assert first.evidence_digest != second.evidence_digest


def test_evidence_digest_binds_available_to_back_ladder() -> None:
    first = assess()
    second = assess(
        book=replace(
            snapshot(),
            available_to_back=(
                PriceSize(Decimal("2.10"), Decimal("4")),
                PriceSize(Decimal("2.04"), Decimal("4")),
                PriceSize(Decimal("2.00"), Decimal("6")),
            ),
        )
    )
    assert first.evidence_digest != second.evidence_digest


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(request(), decision_at=NOW.replace(tzinfo=None))


def test_overlap_key_is_snapshot_ladder_identity_not_wrapper_identity() -> None:
    first = assess()
    second = assess(
        req=replace(
            request(),
            opportunity_id="opp-2",
            opportunity_digest="opp-digest-2",
            action_digest="action-digest-2",
        )
    )
    changed = assess(
        book=replace(
            snapshot(),
            snapshot_digest="book-digest-new",
            market_version=18,
        ),
        req=replace(request(), expected_market_version=18),
    )

    assert first.liquidity_overlap_key == second.liquidity_overlap_key
    assert first.liquidity_overlap_key != changed.liquidity_overlap_key

def test_unbound_market_state_expectations_do_not_create_tautological_checks() -> None:
    result = assess(
        req=replace(
            request(),
            expected_market_version=None,
            expected_inplay=None,
            expected_bet_delay_seconds=None,
        ),
        book=replace(
            snapshot(),
            market_version=18,
            inplay=True,
            bet_delay_seconds=5,
        ),
    )

    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert "PRODUCT_OWNED_EVIDENCE_UNRESOLVED" in result.reasons
    assert "MARKET_VERSION_MISMATCH" not in result.reasons
    assert "INPLAY_MISMATCH" not in result.reasons
    assert "BET_DELAY_MISMATCH" not in result.reasons

