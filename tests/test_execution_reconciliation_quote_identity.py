from __future__ import annotations

from decimal import Decimal

from autosport.bookmaker_receipt_reconciliation import (
    bind_leg_receipt,
    reconcile_equal_split_residual_against_ledger,
)
from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    RoutingState,
    VenueQuote,
)
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.opportunity import QuoteRef
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)


_REQUEST_ID = "quote-identity-falsifier-request"
_PLAN_ID = "quote-identity-falsifier-plan"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-quote-identity",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-21T18:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-21T18:00:00+00:00",
        market_event_hash="a" * 64,
    )


def _venue(source: str, account: str) -> VenueQuote:
    return VenueQuote(source, account, _quote(source), Decimal("100.00"))


def test_copied_leg_id_cannot_hide_different_durable_quote_identity(tmp_path) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = plan_equal_split_residual(
        Decimal("100.00"),
        venues,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )
    leg = initial.legs[0]

    # Keep every routing field currently checked by #852 exact, while changing only
    # durable quote provenance. ExecutionAction.action_id is deliberately copied from
    # the canonical content-derived routing leg id, so action-id equality alone cannot
    # detect this quote-identity substitution.
    durable_action = ExecutionAction(
        action_id=leg.leg_id,
        bookmaker_id=leg.venue.venue_id,
        account_id=leg.venue.account_id,
        event_id=leg.venue.quote.event_id,
        market_id=leg.venue.quote.market_id,
        selection_id=leg.venue.quote.selection_id,
        side="BACK",
        requested_odds=leg.venue.quote.decimal_odds,
        requested_stake=leg.proposed_stake,
        quote_id="b" * 64,
        quote_observed_at=leg.venue.quote.observed_ts,
        expires_at="2026-09-21T18:10:00+00:00",
    )
    ledger = RealExecutionLedger(tmp_path / "quote-identity.jsonl")
    ledger.reserve_plan(
        ExecutionPlan(
            plan_id=_PLAN_ID,
            bookmaker_profile_version="quote-identity-falsifier-v1",
            decision_id="quote-identity-decision",
            approval_id="quote-identity-approval",
            created_at="2026-09-21T18:00:01+00:00",
            actions=(durable_action,),
        )
    )
    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=leg.leg_id,
        attempt_id="attempt-quote-identity",
        reserved_at="2026-09-21T18:00:02+00:00",
    )
    ledger.mark_submitted(
        "attempt-quote-identity",
        submitted_at="2026-09-21T18:00:03+00:00",
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-quote-identity",
            external_receipt_id="receipt-quote-identity",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at="2026-09-21T18:00:04+00:00",
            accepted_odds=leg.venue.quote.decimal_odds,
            accepted_stake=leg.proposed_stake,
        )
    )

    observation = bind_leg_receipt(
        leg,
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-quote-identity",
        confirmed_accepted=leg.proposed_stake,
    )

    try:
        proposal = reconcile_equal_split_residual_against_ledger(
            Decimal("100.00"),
            venues,
            (observation,),
            routing_request_id=_REQUEST_ID,
            parent_plan_id=_PLAN_ID,
            stake_quantum=Decimal("0.01"),
            ledger=ledger,
        )
    except RoutingContractError:
        return

    # A safe implementation may block rather than raise, but a receipt attached to a
    # different durable quote identity must never authorize a new positive stake.
    assert proposal.state is not RoutingState.ROUTE
    assert proposal.proposed_total == Decimal("0")
    assert proposal.legs == ()
