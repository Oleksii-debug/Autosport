from decimal import Decimal

import pytest

from autosport.bookmaker_receipt_reconciliation import bind_leg_receipt
from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    VenueQuote,
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


def test_public_plan_rejects_second_external_receipt_for_same_child() -> None:
    a = _venue("book-a", "acct-a")
    b = _venue("book-b", "acct-b")
    initial = plan_equal_split_residual(
        Decimal("100.00"),
        (a, b),
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )
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
        plan_equal_split_residual(
            Decimal("100.00"),
            (a, b),
            (first, second),
            routing_request_id=_REQUEST_ID,
            parent_plan_id=_PLAN_ID,
            stake_quantum=Decimal("0.01"),
        )
