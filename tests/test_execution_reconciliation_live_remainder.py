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


_REQUEST_ID = "route-live-remainder-1"
_PLAN_ID = "parent-live-remainder-1"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-17T01:40:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-17T01:40:00+00:00",
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


def _execution_plan(initial) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=_PLAN_ID,
        bookmaker_profile_version="live-remainder-falsifier-v1",
        decision_id="routing-decision-1",
        approval_id="routing-approval-1",
        created_at="2026-09-17T01:41:00+00:00",
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
                expires_at="2026-09-17T01:50:00+00:00",
            )
            for leg in initial.legs
        ),
    )


def _ledger_with_partial(tmp_path):
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    ledger.reserve_plan(_execution_plan(initial))

    leg = initial.legs[0]
    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=leg.leg_id,
        attempt_id="attempt-a-1",
        reserved_at="2026-09-17T01:42:00+00:00",
    )
    ledger.mark_submitted(
        "attempt-a-1",
        submitted_at="2026-09-17T01:43:00+00:00",
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-a-1",
            external_receipt_id="external-a-1",
            status=AcknowledgementStatus.PARTIAL,
            acknowledged_at="2026-09-17T01:44:00+00:00",
            accepted_odds=leg.venue.quote.decimal_odds,
            accepted_stake=Decimal("10.00"),
        )
    )
    observation = bind_leg_receipt(
        leg,
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="external-a-1",
        confirmed_accepted=Decimal("10.00"),
    )
    return venues, ledger, observation


def _assert_no_positive_reroute(venues, ledger, observation) -> None:
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

    assert proposal.state is not RoutingState.ROUTE
    assert proposal.proposed_total == Decimal("0")
    assert proposal.legs == ()


def test_exact_partial_amount_without_terminality_proof_cannot_authorize_reroute(tmp_path):
    venues, ledger, observation = _ledger_with_partial(tmp_path)

    # Amount authority is deliberately not the falsifier: caller and durable ACK
    # both say exactly 10. The missing fact is whether the original unmatched
    # remainder can still execute at the provider.
    _assert_no_positive_reroute(venues, ledger, observation)


def test_partial_live_remainder_remains_blocked_after_ledger_reopen(tmp_path):
    venues, ledger, observation = _ledger_with_partial(tmp_path)
    reopened = RealExecutionLedger(ledger.path)

    _assert_no_positive_reroute(venues, reopened, observation)
