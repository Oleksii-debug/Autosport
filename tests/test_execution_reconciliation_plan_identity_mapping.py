from decimal import Decimal

from autosport.bookmaker_receipt_reconciliation import (
    bind_leg_receipt,
    reconcile_equal_split_residual_against_ledger,
)
from autosport.bookmaker_routing import ExternalEffect, RoutingState, VenueQuote
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.opportunity import QuoteRef
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)


_ROUTING_REQUEST_ID = "routing-request-distinct-execution-plan-1"
_ROUTING_PARENT_ID = "portfolio-plan-sha256-routing-parent"
_EXECUTION_PLAN_ID = "supervised-v2-product-issued-execution-plan"


def _quote() -> QuoteRef:
    return QuoteRef(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        source_id="book-a",
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-21T18:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-21T18:00:00+00:00",
        market_event_hash="a" * 64,
    )


def test_routing_parent_identity_may_differ_from_durable_execution_plan_id(
    tmp_path,
) -> None:
    """Canonical routing ancestry must not be conflated with ledger plan identity."""

    venue = VenueQuote("book-a", "acct-a", _quote(), Decimal("100.00"))
    initial = plan_equal_split_residual(
        Decimal("100.00"),
        (venue,),
        routing_request_id=_ROUTING_REQUEST_ID,
        parent_plan_id=_ROUTING_PARENT_ID,
        stake_quantum=Decimal("0.01"),
    )
    assert len(initial.legs) == 1
    leg = initial.legs[0]
    assert leg.parent_plan_id == _ROUTING_PARENT_ID

    action = ExecutionAction(
        action_id=leg.leg_id,
        bookmaker_id=leg.venue.venue_id,
        account_id=leg.venue.account_id,
        event_id=leg.venue.quote.event_id,
        market_id=leg.venue.quote.market_id,
        selection_id=leg.venue.quote.selection_id,
        side="BACK",
        requested_odds=leg.venue.quote.decimal_odds,
        requested_stake=leg.proposed_stake,
        quote_id=leg.venue.quote.market_event_hash,
        quote_observed_at=leg.venue.quote.observed_ts,
        expires_at="2026-09-21T18:10:00+00:00",
    )
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    ledger.reserve_plan(
        ExecutionPlan(
            plan_id=_EXECUTION_PLAN_ID,
            bookmaker_profile_version="supervised-v2",
            decision_id="decision-1",
            approval_id="approval-1",
            created_at="2026-09-21T18:01:00+00:00",
            actions=(action,),
        )
    )
    ledger.begin_attempt(
        plan_id=_EXECUTION_PLAN_ID,
        action_id=leg.leg_id,
        attempt_id="attempt-1",
        reserved_at="2026-09-21T18:02:00+00:00",
    )
    ledger.mark_submitted(
        "attempt-1",
        submitted_at="2026-09-21T18:03:00+00:00",
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-1",
            external_receipt_id="provider-receipt-1",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at="2026-09-21T18:04:00+00:00",
            accepted_odds=leg.venue.quote.decimal_odds,
            accepted_stake=Decimal("100.00"),
        )
    )

    receipt = bind_leg_receipt(
        leg,
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="provider-receipt-1",
        confirmed_accepted=Decimal("100.00"),
    )

    # The canonical routing parent remains the Portfolio/routing identity, while
    # the durable supervised execution plan has its own product-issued identity.
    # Current #852 uses one parent_plan_id for both authorities and therefore
    # rejects this legitimate topology before it can reconcile the exact receipt.
    reconciled = reconcile_equal_split_residual_against_ledger(
        Decimal("100.00"),
        (venue,),
        (receipt,),
        routing_request_id=_ROUTING_REQUEST_ID,
        parent_plan_id=_ROUTING_PARENT_ID,
        stake_quantum=Decimal("0.01"),
        ledger=ledger,
    )

    assert reconciled.state is RoutingState.COMPLETE
    assert reconciled.confirmed_total == Decimal("100.00")
    assert reconciled.residual_before == Decimal("0")
    assert reconciled.proposed_total == Decimal("0")
    assert reconciled.legs == ()
