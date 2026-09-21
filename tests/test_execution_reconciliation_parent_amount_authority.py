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


_REQUEST_ID = "parent-amount-authority-falsifier-request"
_PLAN_ID = "parent-amount-authority-falsifier-plan"
_ORIGINAL_REQUESTED_STAKE = Decimal("100.00")
_REPLAYED_REQUESTED_STAKE = Decimal("200.00")


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-parent-amount-authority",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-21T09:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-21T09:00:00+00:00",
        market_event_hash=("a" if source == "book-a" else "b") * 64,
    )


def _venue(source: str, account: str) -> VenueQuote:
    return VenueQuote(source, account, _quote(source), Decimal("100.00"))


def _initial(venues: tuple[VenueQuote, ...]):
    return plan_equal_split_residual(
        _ORIGINAL_REQUESTED_STAKE,
        venues,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )


def _execution_plan(initial) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=_PLAN_ID,
        bookmaker_profile_version="parent-amount-authority-falsifier-v1",
        decision_id="parent-amount-authority-decision",
        approval_id="parent-amount-authority-approval",
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


def _completed_parent(tmp_path):
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    ledger = RealExecutionLedger(tmp_path / "parent-amount-authority-ledger.jsonl")
    ledger.reserve_plan(_execution_plan(initial))

    observations = []
    for index, leg in enumerate(initial.legs, start=1):
        attempt_id = f"attempt-{index}"
        receipt_id = f"receipt-{index}"
        ledger.begin_attempt(
            plan_id=_PLAN_ID,
            action_id=leg.leg_id,
            attempt_id=attempt_id,
            reserved_at=f"2026-09-21T09:00:0{index + 1}+00:00",
        )
        ledger.mark_submitted(
            attempt_id,
            submitted_at=f"2026-09-21T09:00:0{index + 3}+00:00",
        )
        ledger.acknowledge(
            ExternalAcknowledgement(
                attempt_id=attempt_id,
                external_receipt_id=receipt_id,
                status=AcknowledgementStatus.ACCEPTED,
                acknowledged_at=f"2026-09-21T09:00:0{index + 5}+00:00",
                accepted_odds=leg.venue.quote.decimal_odds,
                accepted_stake=leg.proposed_stake,
            )
        )
        observations.append(
            bind_leg_receipt(
                leg,
                effect=ExternalEffect.ACCEPTED,
                external_receipt_id=receipt_id,
                confirmed_accepted=leg.proposed_stake,
            )
        )

    return venues, initial, tuple(observations), ledger


def _assert_larger_caller_parent_amount_cannot_mint_residual(
    *,
    venues,
    initial,
    observations,
    ledger: RealExecutionLedger,
) -> None:
    try:
        proposal = reconcile_equal_split_residual_against_ledger(
            _REPLAYED_REQUESTED_STAKE,
            venues,
            observations,
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
    assert not {leg.leg_id for leg in initial.legs}.intersection(
        leg.leg_id for leg in proposal.legs
    )


def test_completed_parent_cannot_be_replayed_with_larger_caller_amount(tmp_path) -> None:
    venues, initial, observations, ledger = _completed_parent(tmp_path)

    _assert_larger_caller_parent_amount_cannot_mint_residual(
        venues=venues,
        initial=initial,
        observations=observations,
        ledger=ledger,
    )


def test_restart_does_not_turn_caller_parent_amount_into_routing_authority(tmp_path) -> None:
    venues, initial, observations, ledger = _completed_parent(tmp_path)
    reopened = RealExecutionLedger(ledger.path)

    _assert_larger_caller_parent_amount_cannot_mint_residual(
        venues=venues,
        initial=initial,
        observations=observations,
        ledger=reopened,
    )
