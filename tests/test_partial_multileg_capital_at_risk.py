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
    RoutingCapitalAtRiskTruth,
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
    assert truth.confirmed_routing_notional == Decimal("0")
    assert truth.exact_capital_at_risk is None
    assert truth.non_money_moving_proposed == Decimal("100.00")
    assert truth.unresolved_external_effect is False


def test_partial_multivenue_acceptance_is_not_generic_exact_capital_at_risk() -> None:
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
    assert truth.confirmed_routing_notional == Decimal("20.00")
    assert truth.exact_capital_at_risk is None
    assert truth.non_money_moving_proposed == Decimal("30.00")
    assert truth.unresolved_external_effect is False


def test_unknown_external_effect_preserves_notional_but_no_exact_exposure() -> None:
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
    assert truth.confirmed_routing_notional == Decimal("20.00")
    assert truth.exact_capital_at_risk is None
    assert truth.non_money_moving_proposed == Decimal("0")
    assert truth.unresolved_external_effect is True


def test_sequential_routing_uses_same_fail_closed_exposure_truth() -> None:
    venue = _venue("book-a", "acct-a", "40.00")

    truth = sequential_routing_capital_at_risk_truth(
        Decimal("100.00"),
        (venue,),
        routing_request_id=_REQUEST_ID,
    )

    assert truth.routing_state is RoutingState.ROUTE
    assert truth.confirmed_routing_notional == Decimal("0")
    assert truth.exact_capital_at_risk is None
    assert truth.non_money_moving_proposed == Decimal("40.00")
    assert truth.unresolved_external_effect is False


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


@pytest.mark.parametrize("value", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_direct_truth_rejects_invalid_confirmed_routing_notional(value: Decimal) -> None:
    with pytest.raises(RoutingContractError, match="confirmed_routing_notional"):
        RoutingCapitalAtRiskTruth(
            routing_state=RoutingState.COMPLETE,
            confirmed_routing_notional=value,
            exact_capital_at_risk=None,
            non_money_moving_proposed=Decimal("0"),
            unresolved_external_effect=False,
        )


def test_direct_truth_rejects_caller_minted_exact_capital_at_risk() -> None:
    with pytest.raises(
        RoutingContractError,
        match="generic routing evidence cannot establish exact capital at risk",
    ):
        RoutingCapitalAtRiskTruth(
            routing_state=RoutingState.COMPLETE,
            confirmed_routing_notional=Decimal("20"),
            exact_capital_at_risk=Decimal("20"),
            non_money_moving_proposed=Decimal("0"),
            unresolved_external_effect=False,
        )


@pytest.mark.parametrize(
    ("state", "unresolved"),
    [
        (RoutingState.BLOCKED_UNKNOWN, False),
        (RoutingState.ROUTE, True),
    ],
)
def test_direct_truth_rejects_unknown_state_flag_mismatch(
    state: RoutingState,
    unresolved: bool,
) -> None:
    with pytest.raises(RoutingContractError, match="must match BLOCKED_UNKNOWN"):
        RoutingCapitalAtRiskTruth(
            routing_state=state,
            confirmed_routing_notional=Decimal("0"),
            exact_capital_at_risk=None,
            non_money_moving_proposed=(
                Decimal("0") if state is RoutingState.BLOCKED_UNKNOWN else Decimal("1")
            ),
            unresolved_external_effect=unresolved,
        )


@pytest.mark.parametrize(
    ("state", "proposed"),
    [
        (RoutingState.BLOCKED_UNKNOWN, Decimal("1")),
        (RoutingState.COMPLETE, Decimal("1")),
        (RoutingState.UNEXECUTABLE, Decimal("1")),
        (RoutingState.ROUTE, Decimal("0")),
    ],
)
def test_direct_truth_rejects_state_proposal_inconsistency(
    state: RoutingState,
    proposed: Decimal,
) -> None:
    with pytest.raises(RoutingContractError):
        RoutingCapitalAtRiskTruth(
            routing_state=state,
            confirmed_routing_notional=Decimal("1"),
            exact_capital_at_risk=None,
            non_money_moving_proposed=proposed,
            unresolved_external_effect=state is RoutingState.BLOCKED_UNKNOWN,
        )


def test_direct_truth_rejects_partial_without_notional_or_proposal() -> None:
    with pytest.raises(RoutingContractError, match="PARTIAL"):
        RoutingCapitalAtRiskTruth(
            routing_state=RoutingState.PARTIAL,
            confirmed_routing_notional=Decimal("0"),
            exact_capital_at_risk=None,
            non_money_moving_proposed=Decimal("0"),
            unresolved_external_effect=False,
        )


def test_direct_truth_rejects_unexecutable_with_confirmed_notional() -> None:
    with pytest.raises(RoutingContractError, match="UNEXECUTABLE"):
        RoutingCapitalAtRiskTruth(
            routing_state=RoutingState.UNEXECUTABLE,
            confirmed_routing_notional=Decimal("1"),
            exact_capital_at_risk=None,
            non_money_moving_proposed=Decimal("0"),
            unresolved_external_effect=False,
        )


def test_direct_truth_rejects_complete_without_confirmed_notional() -> None:
    with pytest.raises(RoutingContractError, match="COMPLETE"):
        RoutingCapitalAtRiskTruth(
            routing_state=RoutingState.COMPLETE,
            confirmed_routing_notional=Decimal("0"),
            exact_capital_at_risk=None,
            non_money_moving_proposed=Decimal("0"),
            unresolved_external_effect=False,
        )


def test_direct_truth_rejects_non_boolean_unknown_flag() -> None:
    with pytest.raises(RoutingContractError, match="exact boolean"):
        RoutingCapitalAtRiskTruth(
            routing_state=RoutingState.COMPLETE,
            confirmed_routing_notional=Decimal("20"),
            exact_capital_at_risk=None,
            non_money_moving_proposed=Decimal("0"),
            unresolved_external_effect=1,  # type: ignore[arg-type]
        )
