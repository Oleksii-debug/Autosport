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
    VerifiedExecutionLedgerSnapshot,
)


_REQUEST_ID = "ledger-snapshot-rebind-request"
_PLAN_ID = "ledger-snapshot-rebind-plan"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-ledger-snapshot-rebind",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-21T10:00:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-21T10:00:00+00:00",
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
        bookmaker_profile_version="ledger-snapshot-rebind-v1",
        decision_id="ledger-snapshot-rebind-decision",
        approval_id="ledger-snapshot-rebind-approval",
        created_at="2026-09-21T10:00:01+00:00",
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
                expires_at="2026-09-21T10:10:00+00:00",
            )
            for leg in initial.legs
        ),
    )


def _donor_snapshot(tmp_path, initial) -> VerifiedExecutionLedgerSnapshot:
    donor = RealExecutionLedger(tmp_path / "donor-ledger.jsonl")
    donor.reserve_plan(_execution_plan(initial))
    leg = initial.legs[0]
    donor.begin_attempt(
        plan_id=_PLAN_ID,
        action_id=leg.leg_id,
        attempt_id="donor-attempt-a",
        reserved_at="2026-09-21T10:00:02+00:00",
    )
    donor.mark_submitted(
        "donor-attempt-a",
        submitted_at="2026-09-21T10:00:03+00:00",
    )
    donor.acknowledge(
        ExternalAcknowledgement(
            attempt_id="donor-attempt-a",
            external_receipt_id="donor-receipt-a",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at="2026-09-21T10:00:04+00:00",
            accepted_odds=leg.venue.quote.decimal_odds,
            accepted_stake=leg.proposed_stake,
        )
    )
    return donor.verified_snapshot()


def _receipt(initial):
    return bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="donor-receipt-a",
        confirmed_accepted=initial.legs[0].proposed_stake,
    )


def _assert_no_positive_route(
    *,
    ledger: RealExecutionLedger,
    venues: tuple[VenueQuote, ...],
    initial,
) -> None:
    try:
        result = reconcile_equal_split_residual_against_ledger(
            Decimal("100.00"),
            venues,
            (_receipt(initial),),
            routing_request_id=_REQUEST_ID,
            parent_plan_id=_PLAN_ID,
            stake_quantum=Decimal("0.01"),
            ledger=ledger,
        )
    except (RoutingContractError, TypeError):
        return

    assert not (
        result.state is RoutingState.ROUTE
        and result.proposed_total > Decimal("0")
        and result.legs
    ), "caller-selected snapshot bytes must not mint positive reroute authority"


def test_exact_ledger_instance_cannot_rebind_verified_snapshot_to_other_path(
    tmp_path,
) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    donor_snapshot = _donor_snapshot(tmp_path, initial)

    target_path = tmp_path / "empty-target-ledger.jsonl"
    target = RealExecutionLedger(target_path)
    assert not target_path.exists()

    def rebound_snapshot() -> VerifiedExecutionLedgerSnapshot:
        return donor_snapshot

    # Refusing instance-level method rebinding is itself a valid fail-closed
    # repair. If rebinding remains possible, those donor bytes must not become
    # authority for this empty target ledger identity.
    try:
        target.verified_snapshot = rebound_snapshot  # type: ignore[method-assign]
    except (AttributeError, TypeError):
        return

    _assert_no_positive_route(ledger=target, venues=venues, initial=initial)
    assert not target_path.exists()


class _ReboundSnapshotLedger(RealExecutionLedger):
    def __init__(self, path, snapshot: VerifiedExecutionLedgerSnapshot) -> None:
        super().__init__(path)
        self._snapshot = snapshot

    def verified_snapshot(self) -> VerifiedExecutionLedgerSnapshot:
        return self._snapshot


def test_ledger_subclass_cannot_override_snapshot_authority(tmp_path) -> None:
    venues = (_venue("book-a", "acct-a"), _venue("book-b", "acct-b"))
    initial = _initial(venues)
    donor_snapshot = _donor_snapshot(tmp_path, initial)

    target_path = tmp_path / "empty-subclass-target-ledger.jsonl"
    target = _ReboundSnapshotLedger(target_path, donor_snapshot)
    assert isinstance(target, RealExecutionLedger)
    assert not target_path.exists()

    _assert_no_positive_route(ledger=target, venues=venues, initial=initial)
    assert not target_path.exists()
