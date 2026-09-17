from decimal import Decimal

from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingState,
    VenueObservation,
    VenueQuote,
)
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.opportunity import QuoteRef


_REQUEST_ID = "route-acceptance-1"
_PARENT_PLAN_ID = "parent-plan-acceptance-1"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-acceptance-1",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-17T01:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-17T01:00:00+00:00",
        market_event_hash="b" * 64,
    )


def _venue(source: str, account: str) -> VenueQuote:
    return VenueQuote(source, account, _quote(source), Decimal("100.00"))


def _observation(
    venue: VenueQuote,
    effect: ExternalEffect,
    accepted: str,
    observation_id: str,
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
    venues: tuple[VenueQuote, ...],
    observations: tuple[VenueObservation, ...] = (),
):
    return plan_equal_split_residual(
        Decimal("100.00"),
        venues,
        observations,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PARENT_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )


def test_parent_plan_reconciles_partial_refusal_then_residual_acceptance() -> None:
    venue_a = _venue("book-a", "acct-a")
    venue_b = _venue("book-b", "acct-b")
    selected = (venue_a, venue_b)

    initial = _plan(selected)
    assert initial.state is RoutingState.ROUTE
    assert initial.parent_plan_id == _PARENT_PLAN_ID
    assert [leg.venue for leg in initial.legs] == [venue_a, venue_b]
    assert [leg.proposed_stake for leg in initial.legs] == [
        Decimal("50.00"),
        Decimal("50.00"),
    ]

    after_a = _plan(
        selected,
        (
            _observation(venue_a, ExternalEffect.ACCEPTED, "40.00", "ack-a-1"),
            _observation(venue_a, ExternalEffect.MARKET_REFUSED, "0", "ack-a-2"),
        ),
    )
    assert after_a.state is RoutingState.ROUTE
    assert after_a.parent_plan_id == _PARENT_PLAN_ID
    assert after_a.confirmed_total == Decimal("40.00")
    assert after_a.residual_before == Decimal("60.00")
    assert len(after_a.legs) == 1
    assert after_a.legs[0].venue == venue_b
    assert after_a.legs[0].proposed_stake == Decimal("60.00")

    final = _plan(
        selected,
        (
            _observation(venue_a, ExternalEffect.ACCEPTED, "40.00", "ack-a-1"),
            _observation(venue_a, ExternalEffect.MARKET_REFUSED, "0", "ack-a-2"),
            _observation(venue_b, ExternalEffect.ACCEPTED, "60.00", "ack-b-1"),
        ),
    )
    assert final.state is RoutingState.COMPLETE
    assert final.parent_plan_id == _PARENT_PLAN_ID
    assert final.confirmed_total == Decimal("100.00")
    assert final.residual_before == Decimal("0")
    assert final.proposed_total == Decimal("0")
    assert final.legs == ()
