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
    ExecutionLedgerError,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


_REQUEST_ID = "not-found-authority-falsifier-request"
_PLAN_ID = "not-found-authority-falsifier-plan"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-not-found-authority",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-21T09:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-21T09:00:00+00:00",
        market_event_hash="9" * 64,
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
        bookmaker_profile_version="not-found-authority-falsifier-v1",
        decision_id="not-found-authority-decision",
        approval_id="not-found-authority-approval",
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


def _durable_exploit(tmp_path):
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    assert [leg.proposed_stake for leg in initial.legs] == [
        Decimal("50.00"),
        Decimal("50.00"),
    ]

    path = tmp_path / "not-found-authority-ledger.jsonl"
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(_execution_plan(initial))

    accepted_leg, ambiguous_leg = initial.legs
    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=accepted_leg.leg_id,
        attempt_id="attempt-a-accepted",
        reserved_at="2026-09-21T09:00:02+00:00",
    )
    ledger.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=ambiguous_leg.leg_id,
        attempt_id="attempt-b-unknown",
        reserved_at="2026-09-21T09:00:02+00:00",
    )
    ledger.mark_submitted(
        "attempt-a-accepted",
        submitted_at="2026-09-21T09:00:03+00:00",
    )
    ledger.mark_submitted(
        "attempt-b-unknown",
        submitted_at="2026-09-21T09:00:03+00:00",
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-a-accepted",
            external_receipt_id="receipt-a-accepted",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at="2026-09-21T09:00:04+00:00",
            accepted_odds=accepted_leg.venue.quote.decimal_odds,
            accepted_stake=Decimal("50.00"),
        )
    )
    ledger.mark_unknown(
        "attempt-b-unknown",
        reason="provider-timeout",
        observed_at="2026-09-21T09:00:04+00:00",
    )

    # This is deliberately only a caller-authored DTO. A provider-looking label,
    # fresh timestamp, and persisted event are not product-issued absence proof.
    try:
        ledger.reconcile_not_found(
            ReconciliationSnapshot(
                attempt_id="attempt-b-unknown",
                evidence_id="caller-minted-not-found-evidence",
                observed_at="2026-09-21T09:00:05+00:00",
                external_effect_found=False,
                source="provider-readback",
            )
        )
    except (ExecutionLedgerError, ValueError):
        # An upstream provenance fence is also a valid repair: if the unsafe
        # transition cannot be persisted, it cannot become routing authority.
        return path, ledger, venues, initial, False

    accepted_receipt = bind_leg_receipt(
        accepted_leg,
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="receipt-a-accepted",
        confirmed_accepted=Decimal("50.00"),
    )
    return path, ledger, venues, initial, accepted_receipt


def _assert_not_found_does_not_authorize_positive_reroute(
    *,
    venues: tuple[VenueQuote, ...],
    accepted_receipt,
    ledger: RealExecutionLedger,
) -> None:
    try:
        reconciled = reconcile_equal_split_residual_against_ledger(
            Decimal("100.00"),
            venues,
            (accepted_receipt,),
            routing_request_id=_REQUEST_ID,
            parent_plan_id=_PLAN_ID,
            stake_quantum=Decimal("0.01"),
            ledger=ledger,
        )
    except RoutingContractError:
        # Downstream refusal to consume untrusted NOT_FOUND is valid fail-closed.
        return

    assert reconciled.confirmed_total == Decimal("50.00")
    assert reconciled.residual_before == Decimal("50.00")
    assert reconciled.state is RoutingState.BLOCKED_UNKNOWN
    assert reconciled.proposed_total == Decimal("0")
    assert reconciled.legs == ()


def test_caller_minted_not_found_cannot_authorize_positive_residual_reroute(
    tmp_path,
) -> None:
    _, ledger, venues, _, accepted_receipt = _durable_exploit(tmp_path)
    if accepted_receipt is False:
        return

    _assert_not_found_does_not_authorize_positive_reroute(
        venues=venues,
        accepted_receipt=accepted_receipt,
        ledger=ledger,
    )


def test_restart_does_not_launder_caller_minted_not_found_into_routing_authority(
    tmp_path,
) -> None:
    path, _, venues, _, accepted_receipt = _durable_exploit(tmp_path)
    if accepted_receipt is False:
        return

    # Re-open the durable journal so this falsifier crosses the restart/replay
    # boundary instead of relying on one in-memory ledger instance.
    restarted_ledger = RealExecutionLedger(path)
    _assert_not_found_does_not_authorize_positive_reroute(
        venues=venues,
        accepted_receipt=accepted_receipt,
        ledger=restarted_ledger,
    )
