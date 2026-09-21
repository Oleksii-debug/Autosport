from decimal import Decimal

import pytest

from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingState,
    VenueObservation,
    VenueQuote,
    route_residual,
)
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.capital_at_risk import routing_capital_at_risk_truth
from autosport.opportunity import QuoteRef


_REQUEST_ID = "route-request-capital-risk"
_PLAN_ID = "parent-capital-risk"


def _quote(source_id: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        source_id=source_id,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-21T07:54:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-21T07:54:00+00:00",
        market_event_hash="a" * 64,
    )


def _venue(source_id: str, account_id: str, ceiling: str) -> VenueQuote:
    return VenueQuote(
        venue_id=source_id,
        account_id=account_id,
        quote=_quote(source_id),
        acceptance_ceiling=Decimal(ceiling),
    )


def _observation(
    venue: VenueQuote,
    effect: ExternalEffect,
    *,
    accepted: str = "0",
) -> VenueObservation:
    return VenueObservation(
        venue_id=venue.venue_id,
        account_id=venue.account_id,
        effect=effect,
        routing_request_id=_REQUEST_ID,
        quote=venue.quote,
        confirmed_accepted=Decimal(accepted),
    )


def test_full_non_money_moving_proposal_is_not_capital_already_at_risk() -> None:
    first = _venue("book-a", "acct-a", "100.00")
    second = _venue("book-b", "acct-b", "100.00")

    proposal = plan_equal_split_residual(
        Decimal("100.00"),
        (first, second),
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )
    truth = routing_capital_at_risk_truth(proposal)

    assert proposal.state is RoutingState.ROUTE
    assert proposal.proposed_total == Decimal("100.00")
    assert truth.confirmed_at_risk == Decimal("0")
    assert truth.exact_capital_at_risk == Decimal("0")
    assert truth.non_money_moving_proposed == Decimal("100.00")
    assert truth.unresolved_external_effect is False


def test_partial_multivenue_execution_counts_only_confirmed_accepted_stake() -> None:
    accepted_venue = _venue("book-a", "acct-a", "20.00")
    remaining_venue = _venue("book-b", "acct-b", "30.00")
    accepted = _observation(
        accepted_venue,
        ExternalEffect.ACCEPTED,
        accepted="20.00",
    )

    proposal = plan_equal_split_residual(
        Decimal("100.00"),
        (accepted_venue, remaining_venue),
        (accepted,),
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )
    truth = routing_capital_at_risk_truth(proposal)

    assert proposal.state is RoutingState.PARTIAL
    assert proposal.confirmed_total == Decimal("20.00")
    assert proposal.residual_before == Decimal("80.00")
    assert proposal.proposed_total == Decimal("30.00")
    assert truth.confirmed_at_risk == Decimal("20.00")
    assert truth.exact_capital_at_risk == Decimal("20.00")
    assert truth.non_money_moving_proposed == Decimal("30.00")
    assert truth.unresolved_external_effect is False


def test_unknown_external_effect_with_known_acceptance_has_no_exact_exposure_amount() -> None:
    accepted_venue = _venue("book-a", "acct-a", "20.00")
    uncertain_venue = _venue("book-b", "acct-b", "80.00")
    observations = (
        _observation(
            accepted_venue,
            ExternalEffect.ACCEPTED,
            accepted="20.00",
        ),
        _observation(uncertain_venue, ExternalEffect.UNKNOWN),
    )

    proposal = plan_equal_split_residual(
        Decimal("100.00"),
        (accepted_venue, uncertain_venue),
        observations,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )
    truth = routing_capital_at_risk_truth(proposal)

    assert proposal.state is RoutingState.BLOCKED_UNKNOWN
    assert proposal.confirmed_total == Decimal("20.00")
    assert proposal.proposed_total == Decimal("0")
    assert truth.confirmed_at_risk == Decimal("20.00")
    assert truth.exact_capital_at_risk is None
    assert truth.non_money_moving_proposed == Decimal("0")
    assert truth.unresolved_external_effect is True


def test_low_level_routing_decision_uses_same_capital_at_risk_truth() -> None:
    venue = _venue("book-a", "acct-a", "40.00")

    decision = route_residual(
        Decimal("100.00"),
        (venue,),
        routing_request_id=_REQUEST_ID,
    )
    truth = routing_capital_at_risk_truth(decision)

    assert decision.state is RoutingState.ROUTE
    assert decision.proposed_stake == Decimal("40.00")
    assert truth.confirmed_at_risk == Decimal("0")
    assert truth.exact_capital_at_risk == Decimal("0")
    assert truth.non_money_moving_proposed == Decimal("40.00")


def test_capital_at_risk_truth_rejects_unvalidated_foreign_objects() -> None:
    with pytest.raises(TypeError, match="RoutingDecision or ParallelRoutingProposal"):
        routing_capital_at_risk_truth(object())  # type: ignore[arg-type]
