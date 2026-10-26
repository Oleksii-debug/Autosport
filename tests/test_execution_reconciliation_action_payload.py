from __future__ import annotations

from decimal import Decimal

from autosport.bookmaker_receipt_reconciliation import (
    bind_leg_receipt,
    reconcile_equal_split_residual_against_ledger,
)
from autosport.bookmaker_routing import ExternalEffect, RoutingContractError, RoutingState, VenueQuote
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.opportunity import QuoteRef
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)


_REQUEST_ID = "action-payload-falsifier-request"
_PLAN_ID = "action-payload-falsifier-plan"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-routing-original",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-21T09:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-21T09:00:00+00:00",
        market_event_hash="a" * 64,
    )


def _venue(source: str, account: str) -> VenueQuote:
    return VenueQuote(source, account, _quote(source), Decimal("100.00"))


def _initial(venues: tuple[VenueQuote, ...]):
    return plan_equal_split_residual(
        Decimal("100.00"),
        venues,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )


def _ledger_with_impersonating_action(
    tmp_path,
    initial,
    *,
    mismatch: str,
) -> tuple[RealExecutionLedger, str]:
    canonical_leg = initial.legs[0]
    if mismatch == "selection":
        event_id = canonical_leg.venue.quote.event_id
        market_id = canonical_leg.venue.quote.market_id
        selection_id = "away"
        requested_odds = canonical_leg.venue.quote.decimal_odds
        requested_stake = canonical_leg.proposed_stake
        quote_id = canonical_leg.venue.quote.market_event_hash
    elif mismatch == "stake":
        event_id = canonical_leg.venue.quote.event_id
        market_id = canonical_leg.venue.quote.market_id
        selection_id = canonical_leg.venue.quote.selection_id
        requested_odds = canonical_leg.venue.quote.decimal_odds
        requested_stake = Decimal("10.00")
        quote_id = canonical_leg.venue.quote.market_event_hash
    else:
        raise AssertionError(f"unsupported mismatch: {mismatch}")

    # ExecutionAction.action_id is free-form on this exact head. Deliberately copy
    # the content-derived routing leg_id while changing an economic field.
    durable_action = ExecutionAction(
        action_id=canonical_leg.leg_id,
        bookmaker_id=canonical_leg.venue.venue_id,
        account_id=canonical_leg.venue.account_id,
        event_id=event_id,
        market_id=market_id,
        selection_id=selection_id,
        side="BACK",
        requested_odds=requested_odds,
        requested_stake=requested_stake,
        quote_id=quote_id,
        quote_observed_at=canonical_leg.venue.quote.observed_ts,
        expires_at="2026-09-21T09:10:00+00:00",
    )
    ledger = RealExecutionLedger(tmp_path / f"action-payload-{mismatch}.jsonl")
    ledger.reserve_plan(
        ExecutionPlan(
            plan_id=_PLAN_ID,
            bookmaker_profile_version="action-payload-falsifier-v1",
            decision_id="action-payload-decision",
            approval_id="action-payload-approval",
            created_at="2026-09-21T09:00:01+00:00",
            actions=(durable_action,),
        )
    )
    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=canonical_leg.leg_id,
        attempt_id=f"attempt-{mismatch}",
        reserved_at="2026-09-21T09:00:02+00:00",
    )
    ledger.mark_submitted(
        f"attempt-{mismatch}",
        submitted_at="2026-09-21T09:00:03+00:00",
    )

    # Keep quantity and terminality unambiguous so this falsifier does not depend
    # on the accepted-amount or PARTIAL-live-remainder blockers.
    accepted_stake = requested_stake
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id=f"attempt-{mismatch}",
            external_receipt_id=f"receipt-{mismatch}",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at="2026-09-21T09:00:04+00:00",
            accepted_odds=requested_odds,
            accepted_stake=accepted_stake,
        )
    )
    return ledger, f"receipt-{mismatch}"


def _assert_wrong_durable_action_cannot_authorize_positive_residual(
    *,
    venues: tuple[VenueQuote, ...],
    initial,
    ledger: RealExecutionLedger,
    receipt_id: str,
    confirmed_accepted: Decimal,
) -> None:
    observation = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id=receipt_id,
        confirmed_accepted=confirmed_accepted,
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

    # A fail-closed implementation may return a blocked/zero proposal rather than
    # raising, but a durable action with different economics must never create
    # positive residual-routing authority for this canonical routing leg.
    assert proposal.state is not RoutingState.ROUTE
    assert proposal.proposed_total == Decimal("0")
    assert proposal.legs == ()


def test_copied_action_id_cannot_hide_different_durable_selection(tmp_path) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    ledger, receipt_id = _ledger_with_impersonating_action(
        tmp_path,
        initial,
        mismatch="selection",
    )

    _assert_wrong_durable_action_cannot_authorize_positive_residual(
        venues=venues,
        initial=initial,
        ledger=ledger,
        receipt_id=receipt_id,
        confirmed_accepted=initial.legs[0].proposed_stake,
    )


def test_copied_action_id_cannot_hide_different_durable_requested_stake_after_restart(
    tmp_path,
) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    ledger, receipt_id = _ledger_with_impersonating_action(
        tmp_path,
        initial,
        mismatch="stake",
    )

    reopened = RealExecutionLedger(ledger.path)
    _assert_wrong_durable_action_cannot_authorize_positive_residual(
        venues=venues,
        initial=initial,
        ledger=reopened,
        receipt_id=receipt_id,
        confirmed_accepted=Decimal("10.00"),
    )
