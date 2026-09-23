from decimal import Decimal

import pytest

from autosport.bookmaker_receipt_reconciliation import (
    bind_leg_receipt,
    reconcile_equal_split_residual_against_ledger,
)
from autosport.bookmaker_routing import ExternalEffect, RoutingContractError, VenueQuote
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.opportunity import QuoteRef
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)

REQUEST_ID = "caller-ledger-root-request"
PLAN_ID = "caller-ledger-root-plan"


def _venue(source: str, account: str) -> VenueQuote:
    quote = QuoteRef(
        event_id="event-ledger-root",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-23T00:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-23T00:00:00+00:00",
        market_event_hash="a" * 64,
    )
    return VenueQuote(source, account, quote, Decimal("100.00"))


def _initial(venues):
    return plan_equal_split_residual(
        Decimal("100.00"),
        venues,
        routing_request_id=REQUEST_ID,
        parent_plan_id=PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )


def _plan(initial) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=PLAN_ID,
        bookmaker_profile_version="caller-ledger-root-v1",
        decision_id="caller-ledger-root-decision",
        approval_id="caller-ledger-root-approval",
        created_at="2026-09-23T00:00:01+00:00",
        actions=tuple(
            ExecutionAction(
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
                expires_at="2026-09-23T00:10:00+00:00",
            )
            for leg in initial.legs
        ),
    )


def _ack(ledger, leg, *, attempt_id, receipt_id, status, accepted_stake=None):
    ledger.begin_attempt(
        plan_id=PLAN_ID,
        action_id=leg.leg_id,
        attempt_id=attempt_id,
        reserved_at="2026-09-23T00:00:02+00:00",
    )
    ledger.mark_submitted(attempt_id, submitted_at="2026-09-23T00:00:03+00:00")
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id=attempt_id,
            external_receipt_id=receipt_id,
            status=status,
            acknowledged_at="2026-09-23T00:00:04+00:00",
            accepted_odds=(
                leg.venue.quote.decimal_odds
                if status is AcknowledgementStatus.ACCEPTED
                else None
            ),
            accepted_stake=accepted_stake,
        )
    )


def test_caller_selected_arbitrary_ledger_root_cannot_mint_positive_reroute(tmp_path) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    assert tuple(leg.proposed_stake for leg in initial.legs) == (
        Decimal("50.00"),
        Decimal("50.00"),
    )

    # This exact-type ledger lives at a caller-selected path with no product
    # workspace/root authority. Its bytes are internally canonical only because
    # the same public ledger API was used to manufacture them.
    forged = RealExecutionLedger(tmp_path / "caller-controlled-real-execution.jsonl")
    forged.reserve_plan(_plan(initial))
    _ack(
        forged,
        initial.legs[0],
        attempt_id="caller-attempt-a",
        receipt_id="caller-receipt-a",
        status=AcknowledgementStatus.ACCEPTED,
        accepted_stake=Decimal("50.00"),
    )
    _ack(
        forged,
        initial.legs[1],
        attempt_id="caller-attempt-b",
        receipt_id="caller-receipt-b",
        status=AcknowledgementStatus.REJECTED,
    )

    observations = (
        bind_leg_receipt(
            initial.legs[0],
            effect=ExternalEffect.ACCEPTED,
            external_receipt_id="caller-receipt-a",
            confirmed_accepted=Decimal("50.00"),
        ),
        bind_leg_receipt(
            initial.legs[1],
            effect=ExternalEffect.MARKET_REFUSED,
            external_receipt_id="caller-receipt-b",
        ),
    )

    with pytest.raises(
        RoutingContractError,
        match="workspace|ledger root|trust root|authority|canonical ledger",
    ):
        reconcile_equal_split_residual_against_ledger(
            Decimal("100.00"),
            venues,
            observations,
            routing_request_id=REQUEST_ID,
            parent_plan_id=PLAN_ID,
            stake_quantum=Decimal("0.01"),
            ledger=forged,
        )
