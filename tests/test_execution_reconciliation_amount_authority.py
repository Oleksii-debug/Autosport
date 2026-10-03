from decimal import Decimal

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


_REQUEST_ID = "amount-authority-falsifier-request"
_PLAN_ID = "amount-authority-falsifier-plan"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-amount-authority",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-21T09:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-21T09:00:00+00:00",
        market_event_hash="f" * 64,
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
        bookmaker_profile_version="amount-authority-falsifier-v1",
        decision_id="amount-authority-decision",
        approval_id="amount-authority-approval",
        created_at="2026-09-21T09:00:01+00:00",
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
                expires_at="2026-09-21T09:10:00+00:00",
            )
            for leg in initial.legs
        ),
    )


def _durable_partial(tmp_path, initial, accepted_stake: Decimal):
    path = tmp_path / "amount-authority-ledger.jsonl"
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(_execution_plan(initial))
    leg = initial.legs[0]
    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=leg.leg_id,
        attempt_id="attempt-a",
        reserved_at="2026-09-21T09:00:02+00:00",
    )
    ledger.mark_submitted(
        "attempt-a",
        submitted_at="2026-09-21T09:00:03+00:00",
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-a",
            external_receipt_id="receipt-a",
            status=AcknowledgementStatus.PARTIAL,
            acknowledged_at="2026-09-21T09:00:04+00:00",
            accepted_odds=leg.venue.quote.decimal_odds,
            accepted_stake=accepted_stake,
        )
    )
    return path, ledger


def _assert_caller_amount_is_not_authoritative(
    *,
    venues: tuple[VenueQuote, ...],
    initial,
    ledger: RealExecutionLedger,
    durable_accepted: Decimal,
    caller_accepted: Decimal,
) -> None:
    receipt = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-a",
        confirmed_accepted=caller_accepted,
    )

    try:
        reconciled = reconcile_equal_split_residual_against_ledger(
            Decimal("100.00"),
            venues,
            (receipt,),
            routing_request_id=_REQUEST_ID,
            parent_plan_id=_PLAN_ID,
            stake_quantum=Decimal("0.01"),
            ledger=ledger,
        )
    except RoutingContractError:
        # Exact-match validation is a valid fail-closed implementation.
        return

    # Deriving from durable acknowledgement authority is also valid. What is not
    # valid is letting the caller-authored observation quantity control economics.
    assert reconciled.confirmed_total == durable_accepted
    assert reconciled.residual_before == Decimal("100.00") - durable_accepted


def test_durable_partial_rejects_or_overrides_caller_overstatement(tmp_path) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    _, ledger = _durable_partial(tmp_path, initial, Decimal("10.00"))

    _assert_caller_amount_is_not_authoritative(
        venues=venues,
        initial=initial,
        ledger=ledger,
        durable_accepted=Decimal("10.00"),
        caller_accepted=Decimal("40.00"),
    )


def test_durable_partial_rejects_or_overrides_caller_understatement(tmp_path) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    _, ledger = _durable_partial(tmp_path, initial, Decimal("40.00"))

    _assert_caller_amount_is_not_authoritative(
        venues=venues,
        initial=initial,
        ledger=ledger,
        durable_accepted=Decimal("40.00"),
        caller_accepted=Decimal("10.00"),
    )


def test_restart_does_not_upgrade_replayed_state_into_caller_amount_authority(
    tmp_path,
) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    path, _ = _durable_partial(tmp_path, initial, Decimal("40.00"))

    # Re-open from the durable journal so the falsifier crosses the restart/replay
    # boundary instead of depending on one in-memory ledger object.
    restarted_ledger = RealExecutionLedger(path)
    _assert_caller_amount_is_not_authoritative(
        venues=venues,
        initial=initial,
        ledger=restarted_ledger,
        durable_accepted=Decimal("40.00"),
        caller_accepted=Decimal("50.00"),
    )
