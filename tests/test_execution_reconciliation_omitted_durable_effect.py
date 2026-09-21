from decimal import Decimal

import pytest

from autosport.bookmaker_receipt_reconciliation import (
    bind_leg_receipt,
    reconcile_equal_split_residual_against_ledger,
)
from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
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


_REQUEST_ID = "route-request-omitted-effect-1"
_PLAN_ID = "parent-plan-omitted-effect-1"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-omitted-effect",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-21T12:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-21T12:00:00+00:00",
        market_event_hash="d" * 64,
    )


def _venue(source: str, account: str) -> VenueQuote:
    return VenueQuote(
        source,
        account,
        _quote(source),
        Decimal("100.00"),
    )


def _initial(venues: tuple[VenueQuote, ...]):
    return plan_equal_split_residual(
        Decimal("100.00"),
        venues,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )


def _ledger(tmp_path, initial) -> RealExecutionLedger:
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    actions = tuple(
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
            expires_at="2026-09-21T12:10:00+00:00",
        )
        for leg in initial.legs
    )
    ledger.reserve_plan(
        ExecutionPlan(
            plan_id=_PLAN_ID,
            bookmaker_profile_version="omitted-effect-falsifier-v1",
            decision_id="decision-omitted-effect-1",
            approval_id="approval-omitted-effect-1",
            created_at="2026-09-21T12:01:00+00:00",
            actions=actions,
        )
    )
    return ledger


def _acknowledge(
    ledger: RealExecutionLedger,
    leg,
    *,
    attempt_id: str,
    receipt_id: str,
    status: AcknowledgementStatus,
    accepted_stake: Decimal,
) -> None:
    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=leg.leg_id,
        attempt_id=attempt_id,
        reserved_at="2026-09-21T12:02:00+00:00",
    )
    ledger.mark_submitted(
        attempt_id,
        submitted_at="2026-09-21T12:03:00+00:00",
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id=attempt_id,
            external_receipt_id=receipt_id,
            status=status,
            acknowledged_at="2026-09-21T12:04:00+00:00",
            accepted_odds=leg.venue.quote.decimal_odds,
            accepted_stake=accepted_stake,
        )
    )


def _reconcile(
    venues: tuple[VenueQuote, ...],
    observations,
    ledger: RealExecutionLedger,
):
    return reconcile_equal_split_residual_against_ledger(
        Decimal("100.00"),
        venues,
        observations,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
        ledger=ledger,
    )


def test_durable_accepted_effect_cannot_be_erased_by_empty_observation_set(tmp_path):
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    ledger = _ledger(tmp_path, initial)
    _acknowledge(
        ledger,
        initial.legs[0],
        attempt_id="attempt-a-accepted",
        receipt_id="receipt-a-accepted",
        status=AcknowledgementStatus.ACCEPTED,
        accepted_stake=Decimal("50.00"),
    )

    with pytest.raises(RoutingContractError):
        _reconcile((a, b), (), ledger)


def test_durable_partial_effect_cannot_be_erased_by_empty_observation_set(tmp_path):
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    ledger = _ledger(tmp_path, initial)
    _acknowledge(
        ledger,
        initial.legs[0],
        attempt_id="attempt-a-partial",
        receipt_id="receipt-a-partial",
        status=AcknowledgementStatus.PARTIAL,
        accepted_stake=Decimal("10.00"),
    )

    with pytest.raises(RoutingContractError):
        _reconcile((a, b), (), ledger)


def test_caller_subset_cannot_hide_second_durable_external_effect(tmp_path):
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    ledger = _ledger(tmp_path, initial)

    _acknowledge(
        ledger,
        initial.legs[0],
        attempt_id="attempt-a",
        receipt_id="receipt-a",
        status=AcknowledgementStatus.ACCEPTED,
        accepted_stake=Decimal("50.00"),
    )
    _acknowledge(
        ledger,
        initial.legs[1],
        attempt_id="attempt-b",
        receipt_id="receipt-b",
        status=AcknowledgementStatus.ACCEPTED,
        accepted_stake=Decimal("50.00"),
    )

    only_a = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-a",
        confirmed_accepted=Decimal("50.00"),
    )

    with pytest.raises(RoutingContractError):
        _reconcile((a, b), (only_a,), ledger)


def test_omission_protection_survives_ledger_reopen(tmp_path):
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    path = tmp_path / "real-execution.jsonl"
    ledger = _ledger(tmp_path, initial)
    _acknowledge(
        ledger,
        initial.legs[0],
        attempt_id="attempt-a-restart",
        receipt_id="receipt-a-restart",
        status=AcknowledgementStatus.ACCEPTED,
        accepted_stake=Decimal("50.00"),
    )

    reopened = RealExecutionLedger(path)

    with pytest.raises(RoutingContractError):
        _reconcile((a, b), (), reopened)
