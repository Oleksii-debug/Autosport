from __future__ import annotations

from decimal import Decimal
from pathlib import Path

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
    ExecutionLedgerError,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


_REQUEST_ID = "notfound-authority-routing-request"
_PLAN_ID = "notfound-authority-execution-plan"

_QUOTE_AT = "2026-09-21T18:00:00+00:00"
_PLAN_AT = "2026-09-21T18:00:01+00:00"
_A_RESERVED_AT = "2026-09-21T18:00:02+00:00"
_A_SUBMITTED_AT = "2026-09-21T18:00:03+00:00"
_B_RESERVED_AT = "2026-09-21T18:00:04+00:00"
_B_SUBMITTED_AT = "2026-09-21T18:00:05+00:00"
_B_UNKNOWN_AT = "2026-09-21T18:00:06+00:00"
_A_ACK_AT = "2026-09-21T18:00:07+00:00"
_B_RECONCILED_AT = "2026-09-21T18:00:08+00:00"
_EXPIRES_AT = "2026-09-21T18:10:00+00:00"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-notfound-authority",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts=_QUOTE_AT,
        source_ts=None,
        ingest_ts=_QUOTE_AT,
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
        bookmaker_profile_version="notfound-authority-falsifier-v1",
        decision_id="notfound-authority-decision",
        approval_id="notfound-authority-approval",
        created_at=_PLAN_AT,
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
                expires_at=_EXPIRES_AT,
            )
            for leg in initial.legs
        ),
    )


def _caller_minted_not_found(attempt_id: str) -> ReconciliationSnapshot:
    return ReconciliationSnapshot(
        attempt_id=attempt_id,
        evidence_id="caller-fabricated-provider-absence",
        observed_at=_B_RECONCILED_AT,
        external_effect_found=False,
        source="provider-readback",
    )


def _ledger_with_unsafe_not_found(path: Path):
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    assert len(initial.legs) == 2

    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(_execution_plan(initial))

    a = initial.legs[0]
    b = initial.legs[1]

    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=a.leg_id,
        attempt_id="attempt-a",
        reserved_at=_A_RESERVED_AT,
    )
    ledger.mark_submitted("attempt-a", submitted_at=_A_SUBMITTED_AT)

    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=b.leg_id,
        attempt_id="attempt-b",
        reserved_at=_B_RESERVED_AT,
    )
    ledger.mark_submitted("attempt-b", submitted_at=_B_SUBMITTED_AT)
    ledger.mark_unknown(
        "attempt-b",
        reason="provider_timeout",
        observed_at=_B_UNKNOWN_AT,
    )

    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-a",
            external_receipt_id="receipt-a",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at=_A_ACK_AT,
            accepted_odds=a.venue.quote.decimal_odds,
            accepted_stake=a.proposed_stake,
        )
    )

    # Current #1107 behavior accepts this public caller-shaped DTO and persists
    # RECONCILED_NOT_FOUND. A safe upstream repair may reject it immediately;
    # downstream routing must remain fail-closed in either case.
    try:
        ledger.reconcile_not_found(_caller_minted_not_found("attempt-b"))
    except ExecutionLedgerError:
        pass

    accepted_a = bind_leg_receipt(
        a,
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-a",
        confirmed_accepted=a.proposed_stake,
    )
    return venues, ledger, accepted_a


def _assert_no_positive_reroute(
    venues: tuple[VenueQuote, ...],
    ledger: RealExecutionLedger,
    accepted_a,
) -> None:
    try:
        proposal = reconcile_equal_split_residual_against_ledger(
            Decimal("100.00"),
            venues,
            (accepted_a,),
            routing_request_id=_REQUEST_ID,
            parent_plan_id=_PLAN_ID,
            stake_quantum=Decimal("0.01"),
            ledger=ledger,
        )
    except RoutingContractError:
        return

    # Durable RECONCILED_NOT_FOUND is not itself provider-absence provenance.
    # If the original UNKNOWN effect was released only by caller-authored
    # evidence, no residual stake may be routed. A safe implementation may
    # choose any explicit blocked/non-positive state, but it must grant zero
    # placement authority.
    assert proposal.state is not RoutingState.ROUTE
    assert proposal.proposed_total == Decimal("0")
    assert proposal.legs == ()


def test_caller_minted_not_found_cannot_authorize_residual_reroute(
    tmp_path: Path,
) -> None:
    venues, ledger, accepted_a = _ledger_with_unsafe_not_found(
        tmp_path / "notfound-routing.jsonl"
    )

    _assert_no_positive_reroute(venues, ledger, accepted_a)


def test_caller_minted_not_found_cannot_gain_reroute_authority_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "notfound-routing.jsonl"
    venues, ledger, accepted_a = _ledger_with_unsafe_not_found(path)

    restarted = RealExecutionLedger(ledger.path)
    _assert_no_positive_reroute(venues, restarted, accepted_a)
