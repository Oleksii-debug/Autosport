from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json

import pytest

import autosport.betfair_live_capital_at_risk as live_risk
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_batch_capital_reservation import (
    BetfairBatchCapitalReservation,
    BetfairBatchCapitalReservationError,
    BetfairBatchInstructionReservation,
)
from autosport.betfair_live_capital_at_risk import (
    BetfairLiveCapitalAtRiskEvidence,
    BetfairLiveCapitalAtRiskReason,
    BetfairLiveCapitalAtRiskTruth,
    resolve_betfair_live_capital_at_risk,
)
from autosport.betfair_order_liability import (
    BetfairMarketBettingType,
    BetfairOrderSide,
    BetfairOrderType,
    derive_betfair_order_reserve,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)
from autosport.supervised_execution import (
    BoundSupervisedExecutionPlan,
    ExecutionLegConstraint,
    ProfileBinding,
    _bound_binding_sha256,
)


T0 = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _stable_live_risk_clock(monkeypatch):
    monkeypatch.setattr(
        live_risk,
        "_utc_now",
        lambda: datetime(2026, 9, 22, 0, 0, 20, tzinfo=timezone.utc),
    )


class _Transport:
    def __init__(self, current=(), cleared=None):
        self.current = list(current)
        self.cleared = cleared or {}

    def post(self, url, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        request = json.loads(body)
        method = request["method"]
        if method.endswith("listMarketCatalogue"):
            result = [{"marketId": "1.234", "event": {"id": "event-1"}}]
        elif method.endswith("listCurrentOrders"):
            result = {"currentOrders": self.current, "moreAvailable": False}
        elif method.endswith("listClearedOrders"):
            result = {
                "clearedOrders": self.cleared.get(
                    request["params"]["betStatus"], []
                ),
                "moreAvailable": False,
            }
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode()


def _action(*, action_id="action-1", side="BACK", stake="5"):
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="10",
        side=side,
        requested_odds=Decimal("2"),
        requested_stake=Decimal(stake),
        quote_id="q" * 64,
        quote_observed_at="2026-09-21T23:55:00+00:00",
        expires_at="2026-09-22T01:00:00+00:00",
    )


def _bound(action):
    profile = ProfileBinding(
        "betfair", "acct-1", "betfair-supervised", "1", 1, "a" * 64
    )
    constraint = ExecutionLegConstraint(
        action.action_id,
        action.side,
        "2026-09-22T01:00:00+00:00",
        Decimal("0.05"),
    )
    common = dict(
        bookmaker_profile_version="profile-set-test",
        decision_id="decision-test",
        approval_id="approval-test",
        created_at="2026-09-21T23:56:00+00:00",
        actions=(action,),
    )
    provisional = ExecutionPlan(
        plan_id="pending-supervised-v2-binding", **common
    )
    args = (
        "b" * 64,
        "c" * 64,
        "intent-test",
        "d" * 64,
        "e" * 64,
        (profile,),
        (constraint,),
    )
    bridge = _bound_binding_sha256(provisional, *args)
    execution = ExecutionPlan(plan_id=f"supervised-v2-{bridge}", **common)
    return BoundSupervisedExecutionPlan(execution, *args)


def _context(tmp_path, state):
    action = _action()
    bound = _bound(action)
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    ledger.begin_attempt(
        plan_id=bound.execution_plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at="2026-09-21T23:57:00+00:00",
    )
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id="attempt-1", provider_id="betfair"
    )
    ledger.mark_submitted(
        "attempt-1", submitted_at="2026-09-21T23:58:00+00:00"
    )
    if state == "PARTIAL":
        ledger.acknowledge(
            ExternalAcknowledgement(
                attempt_id="attempt-1",
                external_receipt_id="bet-1",
                status=AcknowledgementStatus.PARTIAL,
                acknowledged_at="2026-09-21T23:59:00+00:00",
                accepted_odds=Decimal("2"),
                accepted_stake=Decimal("2"),
            )
        )
    elif state == "ACCEPTED":
        ledger.acknowledge(
            ExternalAcknowledgement(
                attempt_id="attempt-1",
                external_receipt_id="bet-1",
                status=AcknowledgementStatus.ACCEPTED,
                acknowledged_at="2026-09-21T23:59:00+00:00",
                accepted_odds=Decimal("2"),
                accepted_stake=Decimal("5"),
            )
        )
    else:  # pragma: no cover
        raise AssertionError(state)
    return ledger, bound, provider_ref


def _current(provider_ref, *, matched=2, remaining=1):
    return {
        "betId": "bet-1",
        "marketId": "1.234",
        "selectionId": 10,
        "side": "BACK",
        "status": "EXECUTABLE",
        "placedDate": "2026-09-21T23:58:30+00:00",
        "priceSize": {"price": 2, "size": 5},
        "averagePriceMatched": 2,
        "sizeMatched": matched,
        "sizeRemaining": remaining,
        "customerOrderRef": provider_ref,
    }


def _cleared(provider_ref):
    return "SETTLED", {
        "betId": "bet-1",
        "marketId": "1.234",
        "eventId": "event-1",
        "selectionId": 10,
        "side": "BACK",
        "placedDate": "2026-09-21T23:58:30+00:00",
        "settledDate": "2026-09-22T00:00:00+00:00",
        "priceRequested": 2,
        "priceMatched": 2,
        "sizeSettled": 5,
        "profit": 5,
        "customerOrderRef": provider_ref,
    }


def _capture(provider_ref, *, current=(), cleared=None):
    by_status = {}
    if cleared:
        by_status[cleared[0]] = [cleared[1]]
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=_Transport(current, by_status),
        clock=lambda: T0,
        venue_id="betfair",
        account_id="acct-1",
    )
    return client.read_execution_readback(
        action_id="action-1",
        market_id="1.234",
        provider_order_ref=provider_ref,
    )


def _resolve(ledger, bound, capture):
    return resolve_betfair_live_capital_at_risk(
        ledger, bound, capture, attempt_id="attempt-1"
    )


def _reserve(side, price, size):
    return derive_betfair_order_reserve(
        side=side,
        market_betting_type=BetfairMarketBettingType.ODDS,
        order_type=BetfairOrderType.LIMIT,
        price=Decimal(price),
        size=Decimal(size),
    )


def _member(
    *,
    instruction_id,
    action_id,
    attempt_id,
    reserve,
    evidence=None,
):
    return BetfairBatchInstructionReservation(
        instruction_id=instruction_id,
        action_id=action_id,
        attempt_id=attempt_id,
        requested_reserve=reserve,
        live_risk_evidence=evidence,
    )


def test_unresolved_members_keep_full_reserve_and_never_net():
    back = _member(
        instruction_id="i-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserve=_reserve(BetfairOrderSide.BACK, "2", "5"),
    )
    lay = _member(
        instruction_id="i-2",
        action_id="action-2",
        attempt_id="attempt-2",
        reserve=_reserve(BetfairOrderSide.LAY, "3", "2"),
    )
    batch = BetfairBatchCapitalReservation("batch-1", (back, lay), T0)

    assert back.effective_reserve == Decimal("5")
    assert lay.effective_reserve == Decimal("4")
    assert batch.total_requested_reserve == Decimal("9")
    assert batch.total_reserved == Decimal("9")
    assert batch.all_provider_risk_exact is False
    assert batch.execution_authority is False
    assert batch.to_payload()["cross_member_netting"] is False


def test_partial_current_order_reduces_only_that_member(tmp_path):
    ledger, bound, provider_ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(
            provider_ref,
            current=[_current(provider_ref, matched=2, remaining=1)],
        ),
    )
    evidence.assert_authoritative()
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.EXACT
    assert evidence.capital_at_risk == Decimal("3")

    current = _member(
        instruction_id="i-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserve=_reserve(BetfairOrderSide.BACK, "2", "5"),
        evidence=evidence,
    )
    unresolved = _member(
        instruction_id="i-2",
        action_id="action-2",
        attempt_id="attempt-2",
        reserve=_reserve(BetfairOrderSide.LAY, "3", "2"),
    )
    batch = BetfairBatchCapitalReservation("batch-1", (current, unresolved), T0)

    assert current.effective_reserve == Decimal("3")
    assert unresolved.effective_reserve == Decimal("4")
    assert batch.total_requested_reserve == Decimal("9")
    assert batch.total_reserved == Decimal("7")
    assert batch.all_provider_risk_exact is False


def test_cleared_zero_never_releases_unresolved_sibling(tmp_path):
    ledger, bound, provider_ref = _context(tmp_path, "ACCEPTED")
    evidence = _resolve(
        ledger,
        bound,
        _capture(provider_ref, cleared=_cleared(provider_ref)),
    )
    evidence.assert_authoritative()
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.CLEARED_TERMINAL
    assert evidence.capital_at_risk == Decimal("0")

    cleared = _member(
        instruction_id="i-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserve=_reserve(BetfairOrderSide.BACK, "2", "5"),
        evidence=evidence,
    )
    unresolved = _member(
        instruction_id="i-2",
        action_id="action-2",
        attempt_id="attempt-2",
        reserve=_reserve(BetfairOrderSide.LAY, "3", "2"),
    )
    batch = BetfairBatchCapitalReservation("batch-1", (cleared, unresolved), T0)

    assert cleared.effective_reserve == Decimal("0")
    assert batch.total_reserved == Decimal("4")
    assert batch.all_provider_risk_exact is False


def test_canonical_unknown_evidence_keeps_full_reserve(tmp_path):
    ledger, bound, provider_ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(ledger, bound, _capture(provider_ref))
    evidence.assert_authoritative()
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.UNKNOWN

    member = _member(
        instruction_id="i-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserve=_reserve(BetfairOrderSide.BACK, "2", "5"),
        evidence=evidence,
    )
    batch = BetfairBatchCapitalReservation("batch-1", (member,), T0)

    assert member.effective_reserve == Decimal("5")
    assert member.provider_risk_exact is False
    assert batch.total_reserved == Decimal("5")
    assert batch.all_provider_risk_exact is False


def test_batch_as_of_cannot_precede_provider_readback(tmp_path):
    ledger, bound, provider_ref = _context(tmp_path, "ACCEPTED")
    evidence = _resolve(
        ledger,
        bound,
        _capture(provider_ref, cleared=_cleared(provider_ref)),
    )
    member = _member(
        instruction_id="i-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserve=_reserve(BetfairOrderSide.BACK, "2", "5"),
        evidence=evidence,
    )

    with pytest.raises(
        BetfairBatchCapitalReservationError,
        match="as_of cannot precede provider readback",
    ):
        BetfairBatchCapitalReservation(
            "batch-1",
            (member,),
            datetime(2026, 9, 21, 23, 59, 59, tzinfo=timezone.utc),
        )


def test_direct_caller_constructed_live_risk_cannot_reduce_reserve():
    forged = BetfairLiveCapitalAtRiskEvidence(
        truth=BetfairLiveCapitalAtRiskTruth.EXACT,
        reason=BetfairLiveCapitalAtRiskReason.CLEARED_TERMINAL,
        capital_at_risk=Decimal("0"),
        plan_id="plan-1",
        attempt_id="attempt-1",
        attempt_state=AttemptState.ACCEPTED,
        action_id="action-1",
        provider_order_ref="provider-ref",
        bet_id="bet-1",
        readback_observed_at="2026-09-22T00:00:00+00:00",
        provider_row_observed_at=None,
        readback_request_scope_sha256="a" * 64,
        readback_evidence_sha256="b" * 64,
        ledger_snapshot_sha256="c" * 64,
    )
    with pytest.raises(
        BetfairBatchCapitalReservationError,
        match="not current canonical provider evidence",
    ):
        _member(
            instruction_id="i-1",
            action_id="action-1",
            attempt_id="attempt-1",
            reserve=_reserve(BetfairOrderSide.BACK, "2", "5"),
            evidence=forged,
        )


def test_authoritative_evidence_cannot_be_rebound_to_another_member(tmp_path):
    ledger, bound, provider_ref = _context(tmp_path, "ACCEPTED")
    evidence = _resolve(
        ledger,
        bound,
        _capture(provider_ref, cleared=_cleared(provider_ref)),
    )
    with pytest.raises(
        BetfairBatchCapitalReservationError,
        match="identity does not match",
    ):
        _member(
            instruction_id="i-2",
            action_id="action-2",
            attempt_id="attempt-2",
            reserve=_reserve(BetfairOrderSide.BACK, "2", "5"),
            evidence=evidence,
        )


def test_requested_reserve_mutation_is_revalidated_on_read():
    reserve = _reserve(BetfairOrderSide.BACK, "2", "5")
    member = _member(
        instruction_id="i-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserve=reserve,
    )
    object.__setattr__(reserve, "reserve", Decimal("0"))

    with pytest.raises(
        BetfairBatchCapitalReservationError,
        match="no longer matches canonical liability derivation",
    ):
        _ = member.effective_reserve


def test_duplicate_member_identities_fail_closed():
    first = _member(
        instruction_id="i-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserve=_reserve(BetfairOrderSide.BACK, "2", "5"),
    )
    same_instruction = _member(
        instruction_id="i-1",
        action_id="action-2",
        attempt_id="attempt-2",
        reserve=_reserve(BetfairOrderSide.BACK, "2", "1"),
    )
    with pytest.raises(
        BetfairBatchCapitalReservationError,
        match="instruction identities must be unique",
    ):
        BetfairBatchCapitalReservation(
            "batch-1", (first, same_instruction), T0
        )


def test_total_is_exact_under_low_decimal_precision():
    one = _member(
        instruction_id="i-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserve=_reserve(
            BetfairOrderSide.BACK,
            "2",
            "999999999999999999999999999999.99",
        ),
    )
    two = _member(
        instruction_id="i-2",
        action_id="action-2",
        attempt_id="attempt-2",
        reserve=_reserve(BetfairOrderSide.BACK, "2", "0.02"),
    )
    with localcontext() as context:
        context.prec = 6
        batch = BetfairBatchCapitalReservation("batch-1", (one, two), T0)
        assert (
            batch.total_reserved
            == Decimal("1000000000000000000000000000000.01")
        )


def test_evidence_identity_binds_member_order_and_as_of():
    one = _member(
        instruction_id="i-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserve=_reserve(BetfairOrderSide.BACK, "2", "5"),
    )
    two = _member(
        instruction_id="i-2",
        action_id="action-2",
        attempt_id="attempt-2",
        reserve=_reserve(BetfairOrderSide.LAY, "3", "2"),
    )
    forward = BetfairBatchCapitalReservation("batch-1", (one, two), T0)
    reverse = BetfairBatchCapitalReservation("batch-1", (two, one), T0)
    later = BetfairBatchCapitalReservation(
        "batch-1",
        (one, two),
        datetime(2026, 9, 22, 0, 0, 1, tzinfo=timezone.utc),
    )

    assert forward.evidence_id != reverse.evidence_id
    assert forward.evidence_id != later.evidence_id
    assert len(forward.evidence_id) == 64
