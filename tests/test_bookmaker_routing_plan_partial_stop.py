from decimal import Decimal

import pytest

from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    RoutingState,
    VenueObservation,
    VenueQuote,
)
from autosport.bookmaker_routing_plan import (
    ParallelRoutingProposal,
    plan_equal_split_residual,
)
from autosport.opportunity import QuoteRef


def _quote(source_id: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        source_id=source_id,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-16T20:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-16T20:00:00+00:00",
        market_event_hash="a" * 64,
    )


def test_confirmed_partial_with_no_remaining_selected_capacity_stops_explicitly() -> None:
    venue = VenueQuote(
        venue_id="book-a",
        account_id="acct-a",
        quote=_quote("book-a"),
        acceptance_ceiling=Decimal("40.00"),
    )
    accepted = VenueObservation(
        venue_id=venue.venue_id,
        account_id=venue.account_id,
        effect=ExternalEffect.ACCEPTED,
        routing_request_id="route-request-1",
        quote=venue.quote,
        confirmed_accepted=Decimal("40.00"),
    )

    proposal = plan_equal_split_residual(
        Decimal("100.00"),
        (venue,),
        (accepted,),
        routing_request_id="route-request-1",
        parent_plan_id="parent-plan-1",
        stake_quantum=Decimal("0.01"),
    )

    assert proposal.state is RoutingState.PARTIAL
    assert proposal.confirmed_total == Decimal("40.00")
    assert proposal.residual_before == Decimal("60.00")
    assert proposal.proposed_total == Decimal("0")
    assert proposal.legs == ()


def test_zero_proposal_partial_without_prior_confirmation_remains_invalid() -> None:
    with pytest.raises(RoutingContractError, match="prior confirmed stake"):
        ParallelRoutingProposal(
            state=RoutingState.PARTIAL,
            parent_plan_id="parent-plan-1",
            routing_request_id="route-request-1",
            residual_before=Decimal("60.00"),
            confirmed_total=Decimal("0"),
            proposed_total=Decimal("0"),
            stake_quantum=Decimal("0.01"),
            legs=(),
        )
