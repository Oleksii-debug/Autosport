from decimal import Decimal

import pytest

from autosport.bookmaker_receipt_reconciliation import bind_leg_receipt
from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    VenueObservation,
    VenueQuote,
    route_residual,
)
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.opportunity import QuoteRef


_REQUEST_ID = "route-request-public-receipt-guard"
_PLAN_ID = "parent-plan-public-receipt-guard"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-17T02:16:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-17T02:16:00+00:00",
        market_event_hash="a" * 64,
    )


def _venue(source: str, account: str) -> VenueQuote:
    return VenueQuote(source, account, _quote(source), Decimal("100.00"))


def _initial(a: VenueQuote, b: VenueQuote):
    return plan_equal_split_residual(
        Decimal("100.00"),
        (a, b),
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )


def _plan(a: VenueQuote, b: VenueQuote, observations):
    return plan_equal_split_residual(
        Decimal("100.00"),
        (a, b),
        observations,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )


def test_public_plan_rejects_second_external_receipt_for_same_child() -> None:
    a = _venue("book-a", "acct-a")
    b = _venue("book-b", "acct-b")
    initial = _initial(a, b)
    first = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-a-1",
        confirmed_accepted=Decimal("50.00"),
    )
    second = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-a-2",
        confirmed_accepted=Decimal("50.00"),
    )

    with pytest.raises(RoutingContractError, match="multiple external receipts"):
        _plan(a, b, (first, second))


def test_public_plan_exact_receipt_replay_is_allocation_idempotent() -> None:
    a = _venue("book-a", "acct-a")
    b = _venue("book-b", "acct-b")
    initial = _initial(a, b)
    receipt = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-a-1",
        confirmed_accepted=Decimal("50.00"),
    )

    single = _plan(a, b, (receipt,))
    replayed = _plan(a, b, (receipt, receipt))

    assert replayed == single


def test_lower_level_route_rejects_forged_child_id_for_same_binding() -> None:
    a = _venue("book-a", "acct-a")
    b = _venue("book-b", "acct-b")
    initial = _initial(a, b)
    canonical = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-a-1",
        confirmed_accepted=Decimal("50.00"),
    )
    assert canonical.proposal_leg_id is not None
    forged_leg_id = (
        ("0" if canonical.proposal_leg_id[0] != "0" else "1")
        + canonical.proposal_leg_id[1:]
    )
    forged = VenueObservation(
        venue_id=canonical.venue_id,
        account_id=canonical.account_id,
        effect=ExternalEffect.ACCEPTED,
        routing_request_id=canonical.routing_request_id,
        quote=canonical.quote,
        confirmed_accepted=canonical.confirmed_accepted,
        parent_plan_id=canonical.parent_plan_id,
        proposal_leg_id=forged_leg_id,
        proposed_stake=canonical.proposed_stake,
        external_receipt_id="receipt-a-forged",
    )

    with pytest.raises(
        RoutingContractError,
        match="leg_id does not match canonical proposal identity",
    ):
        route_residual(
            Decimal("100.00"),
            (a, b),
            (canonical, forged),
            routing_request_id=_REQUEST_ID,
        )
