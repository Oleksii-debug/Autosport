from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import autosport.bookmaker_receipt_reconciliation as reconciliation_module
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
    AttemptState,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)


_REQUEST_ID = "snapshot-race-request"
_PLAN_ID = "snapshot-race-plan"
_BASE = "2026-09-21T09:"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-snapshot-race",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts=f"{_BASE}00:00+00:00",
        source_ts=None,
        ingest_ts=f"{_BASE}00:00+00:00",
        market_event_hash="d" * 64,
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
        bookmaker_profile_version="snapshot-race-v1",
        decision_id="snapshot-race-decision",
        approval_id="snapshot-race-approval",
        created_at=f"{_BASE}01:00+00:00",
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
                expires_at=f"{_BASE}59:00+00:00",
            )
            for leg in initial.legs
        ),
    )


def _ledger_with_first_leg_accepted(tmp_path, initial) -> RealExecutionLedger:
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    ledger.reserve_plan(_execution_plan(initial))
    first = initial.legs[0]
    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=first.leg_id,
        attempt_id="attempt-a",
        reserved_at=f"{_BASE}02:00+00:00",
    )
    ledger.mark_submitted(
        "attempt-a",
        submitted_at=f"{_BASE}03:00+00:00",
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-a",
            external_receipt_id="receipt-a",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at=f"{_BASE}04:00+00:00",
            accepted_odds=first.venue.quote.decimal_odds,
            accepted_stake=first.proposed_stake,
        )
    )
    return ledger


def _first_receipt(initial):
    first = initial.legs[0]
    return bind_leg_receipt(
        first,
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-a",
        confirmed_accepted=first.proposed_stake,
    )


def _reconcile(venues, receipt, ledger):
    return reconcile_equal_split_residual_against_ledger(
        Decimal("100.00"),
        venues,
        (receipt,),
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
        ledger=ledger,
    )


def _assert_no_positive_route(proposal) -> None:
    assert proposal.state is not RoutingState.ROUTE
    assert proposal.proposed_total == Decimal("0")
    assert proposal.legs == ()


def test_new_reserved_attempt_after_verified_snapshot_cannot_leave_positive_reroute(
    tmp_path,
) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    ledger = _ledger_with_first_leg_accepted(tmp_path, initial)
    receipt = _first_receipt(initial)
    second = initial.legs[1]
    writer_ledger = RealExecutionLedger(ledger.path)
    original_verified_events = reconciliation_module._verified_ledger_events
    injected = False

    def read_then_reserve(candidate_ledger):
        nonlocal injected
        events = original_verified_events(candidate_ledger)
        if not injected:
            injected = True
            writer_ledger.begin_attempt(
                plan_id=_PLAN_ID,
                action_id=second.leg_id,
                attempt_id="attempt-race-reserved",
                reserved_at=f"{_BASE}05:00+00:00",
            )
        return events

    try:
        with patch.object(
            reconciliation_module,
            "_verified_ledger_events",
            side_effect=read_then_reserve,
        ):
            proposal = _reconcile(venues, receipt, ledger)
    except RoutingContractError:
        proposal = None

    assert injected
    assert ledger.attempt_state("attempt-race-reserved") is AttemptState.RESERVED
    if proposal is not None:
        _assert_no_positive_route(proposal)


def test_new_accepted_effect_after_verified_snapshot_cannot_be_omitted_from_reroute(
    tmp_path,
) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    ledger = _ledger_with_first_leg_accepted(tmp_path, initial)
    receipt = _first_receipt(initial)
    second = initial.legs[1]
    writer_ledger = RealExecutionLedger(ledger.path)
    original_verified_events = reconciliation_module._verified_ledger_events
    injected = False

    def read_then_accept_second_leg(candidate_ledger):
        nonlocal injected
        events = original_verified_events(candidate_ledger)
        if not injected:
            injected = True
            writer_ledger.begin_attempt(
                plan_id=_PLAN_ID,
                action_id=second.leg_id,
                attempt_id="attempt-race-accepted",
                reserved_at=f"{_BASE}05:00+00:00",
            )
            writer_ledger.mark_submitted(
                "attempt-race-accepted",
                submitted_at=f"{_BASE}06:00+00:00",
            )
            writer_ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="attempt-race-accepted",
                    external_receipt_id="receipt-race-accepted",
                    status=AcknowledgementStatus.ACCEPTED,
                    acknowledged_at=f"{_BASE}07:00+00:00",
                    accepted_odds=second.venue.quote.decimal_odds,
                    accepted_stake=second.proposed_stake,
                )
            )
        return events

    try:
        with patch.object(
            reconciliation_module,
            "_verified_ledger_events",
            side_effect=read_then_accept_second_leg,
        ):
            proposal = _reconcile(venues, receipt, ledger)
    except RoutingContractError:
        proposal = None

    assert injected
    assert ledger.attempt_state("attempt-race-accepted") is AttemptState.ACCEPTED
    if proposal is not None:
        _assert_no_positive_route(proposal)
