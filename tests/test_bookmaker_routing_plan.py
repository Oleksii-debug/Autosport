from decimal import Decimal

import pytest

from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    RoutingState,
    VenueObservation,
    VenueQuote,
)
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.opportunity import QuoteRef


_REQUEST_ID = "route-request-1"
_PLAN_ID = "parent-plan-1"


def _quote(source: str, selection: str = "home") -> QuoteRef:
    return QuoteRef(
        event_id="event-1",
        market_id="winner",
        selection_id=selection,
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-16T19:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-16T19:00:00+00:00",
        market_event_hash="a" * 64,
    )


def _venue(source: str, account: str, ceiling: str = "100") -> VenueQuote:
    return VenueQuote(source, account, _quote(source), Decimal(ceiling))


def _observation(
    venue: VenueQuote,
    effect: ExternalEffect,
    accepted: str = "0",
    observation_id: str | None = None,
) -> VenueObservation:
    return VenueObservation(
        venue_id=venue.venue_id,
        account_id=venue.account_id,
        effect=effect,
        routing_request_id=_REQUEST_ID,
        quote=venue.quote,
        confirmed_accepted=Decimal(accepted),
        observation_id=observation_id,
    )


def _plan(
    requested: str,
    venues: tuple[VenueQuote, ...],
    observations: tuple[VenueObservation, ...] = (),
    *,
    quantum: str = "0.01",
):
    return plan_equal_split_residual(
        Decimal(requested),
        venues,
        observations,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal(quantum),
    )


def test_equal_split_builds_two_deterministic_children_under_one_parent() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")

    first = _plan("100.00", (a, b))
    second = _plan("100.00", (a, b))

    assert first.state is RoutingState.ROUTE
    assert first.parent_plan_id == _PLAN_ID
    assert first.routing_request_id == _REQUEST_ID
    assert first.residual_before == Decimal("100.00")
    assert first.proposed_total == Decimal("100.00")
    assert [leg.proposed_stake for leg in first.legs] == [
        Decimal("50.00"),
        Decimal("50.00"),
    ]
    assert [leg.venue for leg in first.legs] == [a, b]
    assert len({leg.leg_id for leg in first.legs}) == 2
    assert [leg.leg_id for leg in first.legs] == [
        leg.leg_id for leg in second.legs
    ]
    assert all(leg.parent_plan_id == _PLAN_ID for leg in first.legs)


def test_one_quantum_remainder_uses_selected_venue_order_deterministically() -> None:
    a = _venue("book-a", "acct-a")
    b = _venue("book-b", "acct-b")
    c = _venue("book-c", "acct-c")

    proposal = _plan("100.00", (a, b, c))

    assert proposal.state is RoutingState.ROUTE
    assert [leg.proposed_stake for leg in proposal.legs] == [
        Decimal("33.34"),
        Decimal("33.33"),
        Decimal("33.33"),
    ]
    assert proposal.proposed_total == Decimal("100.00")


def test_water_fill_respects_low_venue_ceiling_and_redistributes_residual() -> None:
    a = _venue("book-a", "acct-a", "20.00")
    b = _venue("book-b", "acct-b", "100.00")

    proposal = _plan("100.00", (a, b))

    assert proposal.state is RoutingState.ROUTE
    assert [leg.proposed_stake for leg in proposal.legs] == [
        Decimal("20.00"),
        Decimal("80.00"),
    ]


def test_confirmed_then_refused_venue_is_not_reused_in_parallel_residual() -> None:
    a = _venue("book-a", "acct-a", "100.00")
    b = _venue("book-b", "acct-b", "100.00")
    c = _venue("book-c", "acct-c", "100.00")
    observations = (
        _observation(a, ExternalEffect.ACCEPTED, "20.00", "ack-a-1"),
        _observation(a, ExternalEffect.MARKET_REFUSED, observation_id="ack-a-2"),
    )

    proposal = _plan("100.00", (a, b, c), observations)

    assert proposal.confirmed_total == Decimal("20.00")
    assert proposal.residual_before == Decimal("80.00")
    assert proposal.state is RoutingState.ROUTE
    assert [leg.venue for leg in proposal.legs] == [b, c]
    assert [leg.proposed_stake for leg in proposal.legs] == [
        Decimal("40.00"),
        Decimal("40.00"),
    ]


def test_unknown_external_effect_blocks_all_parallel_children() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")

    proposal = _plan(
        "100.00",
        (a, b),
        (_observation(a, ExternalEffect.UNKNOWN),),
    )

    assert proposal.state is RoutingState.BLOCKED_UNKNOWN
    assert proposal.proposed_total == Decimal("0")
    assert proposal.legs == ()


def test_insufficient_selected_capacity_is_explicit_partial_proposal() -> None:
    a = _venue("book-a", "acct-a", "20.00")
    b = _venue("book-b", "acct-b", "30.00")

    proposal = _plan("100.00", (a, b))

    assert proposal.state is RoutingState.PARTIAL
    assert proposal.residual_before == Decimal("100.00")
    assert proposal.proposed_total == Decimal("50.00")
    assert [leg.proposed_stake for leg in proposal.legs] == [
        Decimal("20.00"),
        Decimal("30.00"),
    ]


def test_disabled_and_refused_venues_cannot_enlarge_parallel_authority() -> None:
    disabled = VenueQuote(
        "book-a",
        "acct-a",
        _quote("book-a"),
        Decimal("100.00"),
        account_enabled=False,
    )
    refused = _venue("book-b", "acct-b")
    eligible = _venue("book-c", "acct-c", "25.00")

    proposal = _plan(
        "50.00",
        (disabled, refused, eligible),
        (_observation(refused, ExternalEffect.MARKET_REFUSED),),
    )

    assert proposal.state is RoutingState.PARTIAL
    assert [leg.venue for leg in proposal.legs] == [eligible]
    assert proposal.proposed_total == Decimal("25.00")


def test_requested_residual_must_be_exact_multiple_of_explicit_quantum() -> None:
    a = _venue("book-a", "acct-a")

    with pytest.raises(RoutingContractError, match="exact multiple"):
        _plan("10.005", (a,), quantum="0.01")


def test_zero_or_non_decimal_quantum_fails_closed() -> None:
    a = _venue("book-a", "acct-a")

    with pytest.raises(RoutingContractError, match="stake_quantum"):
        plan_equal_split_residual(
            Decimal("10.00"),
            (a,),
            routing_request_id=_REQUEST_ID,
            parent_plan_id=_PLAN_ID,
            stake_quantum=Decimal("0"),
        )

    with pytest.raises(RoutingContractError, match="stake_quantum"):
        plan_equal_split_residual(
            Decimal("10.00"),
            (a,),
            routing_request_id=_REQUEST_ID,
            parent_plan_id=_PLAN_ID,
            stake_quantum=1,  # type: ignore[arg-type]
        )


def test_complete_request_has_no_new_child_proposals() -> None:
    a = _venue("book-a", "acct-a")

    proposal = _plan(
        "40.00",
        (a,),
        (_observation(a, ExternalEffect.ACCEPTED, "40.00"),),
    )

    assert proposal.state is RoutingState.COMPLETE
    assert proposal.residual_before == Decimal("0")
    assert proposal.proposed_total == Decimal("0")
    assert proposal.legs == ()
