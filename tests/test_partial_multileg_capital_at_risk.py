from decimal import Decimal

import pytest

from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    RoutingState,
    VenueObservation,
    VenueQuote,
)
from autosport.capital_at_risk import (
    parallel_routing_capital_at_risk_truth,
    sequential_routing_capital_at_risk_truth,
)
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

    truth = parallel_routing_capital_at_risk_truth(
        Decimal("100.00"),
        (first, second),
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )

    assert truth.routing_state is RoutingState.ROUTE
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

    truth = parallel_routing_capital_at_risk_truth(
        Decimal("100.00"),
        (accepted_venue, remaining_venue),
        (accepted,),
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )

    assert truth.routing_state is RoutingState.PARTIAL
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

    truth = parallel_routing_capital_at_risk_truth(
        Decimal("100.00"),
        (accepted_venue, uncertain_venue),
        observations,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )

    assert truth.routing_state is RoutingState.BLOCKED_UNKNOWN
    assert truth.confirmed_at_risk == Decimal("20.00")
    assert truth.exact_capital_at_risk is None
    assert truth.non_money_moving_proposed == Decimal("0")
    assert truth.unresolved_external_effect is True


def test_sequential_routing_uses_same_capital_at_risk_truth() -> None:
    venue = _venue("book-a", "acct-a", "40.00")

    truth = sequential_routing_capital_at_risk_truth(
        Decimal("100.00"),
        (venue,),
        routing_request_id=_REQUEST_ID,
    )

    assert truth.routing_state is RoutingState.ROUTE
    assert truth.confirmed_at_risk == Decimal("0")
    assert truth.exact_capital_at_risk == Decimal("0")
    assert truth.non_money_moving_proposed == Decimal("40.00")


def test_truth_wrapper_preserves_canonical_fail_closed_validation() -> None:
    selected = _venue("book-a", "acct-a", "40.00")
    foreign = _venue("book-b", "acct-b", "40.00")
    invalid_observation = _observation(foreign, ExternalEffect.UNKNOWN)

    with pytest.raises(RoutingContractError, match="unselected venue/account"):
        sequential_routing_capital_at_risk_truth(
            Decimal("100.00"),
            (selected,),
            (invalid_observation,),
            routing_request_id=_REQUEST_ID,
        )
