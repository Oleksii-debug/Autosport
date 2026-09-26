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


_REQUEST_ID = "unattempted-action-request"
_PLAN_ID = "unattempted-action-plan"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-unattempted-action",
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
    return VenueQuote(
        source,
        account,
        _quote(source),
        Decimal("50.00"),
    )


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
        bookmaker_profile_version="unattempted-action-falsifier-v1",
        decision_id="unattempted-action-decision",
        approval_id="unattempted-action-approval",
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


def _assert_no_positive_reroute(
    *,
    venues: tuple[VenueQuote, ...],
    ledger: RealExecutionLedger,
    observations=(),
) -> None:
    try:
        proposal = reconcile_equal_split_residual_against_ledger(
            Decimal("100.00"),
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


def test_plan_reserved_actions_without_attempts_cannot_be_reissued(tmp_path) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    assert [leg.proposed_stake for leg in initial.legs] == [
        Decimal("50.00"),
        Decimal("50.00"),
    ]

    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    ledger.reserve_plan(_execution_plan(initial))

    # The durable execution plan already owns both actions. Neither has a terminal
    # no-effect disposition, so replaying the original routing proposal would create
    # concurrent authority for actions that the execution plan may still start.
    _assert_no_positive_reroute(
        venues=venues,
        ledger=ledger,
        observations=(),
    )


def test_unattempted_sibling_stays_blocked_after_other_child_is_accepted(
    tmp_path,
) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    ledger.reserve_plan(_execution_plan(initial))

    accepted_leg = initial.legs[0]
    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=accepted_leg.leg_id,
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
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at="2026-09-21T09:00:04+00:00",
            accepted_odds=accepted_leg.venue.quote.decimal_odds,
            accepted_stake=accepted_leg.proposed_stake,
        )
    )
    observation = bind_leg_receipt(
        accepted_leg,
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-a",
        confirmed_accepted=accepted_leg.proposed_stake,
    )

    # Action B is already present in PLAN_RESERVED but has never acquired an attempt
    # or terminal no-effect resolution. It must not be emitted again as a new positive
    # residual proposal while that durable action can still be started.
    _assert_no_positive_reroute(
        venues=venues,
        ledger=ledger,
        observations=(observation,),
    )


if __name__ == "__main__":
    raise SystemExit("run with pytest")
