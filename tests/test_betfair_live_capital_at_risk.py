from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json

import pytest

import autosport.betfair_live_capital_at_risk as live_risk
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_live_capital_at_risk import (
    BetfairLiveCapitalAtRiskError,
    BetfairLiveCapitalAtRiskReason,
    BetfairLiveCapitalAtRiskTruth,
    _exact_add,
    resolve_betfair_live_capital_at_risk,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
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


def _action(side="BACK", stake="5"):
    return ExecutionAction(
        action_id="action-1",
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
        "action-1",
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


def _context(tmp_path, state, *, side="BACK", stake="5"):
    action = _action(side, stake)
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
    if state == "UNKNOWN":
        ledger.mark_unknown(
            "attempt-1",
            reason="provider_effect_requires_readback",
            observed_at="2026-09-21T23:59:00+00:00",
        )
    elif state in {"PARTIAL", "ACCEPTED", "REJECTED"}:
        status = AcknowledgementStatus(state)
        kwargs = {}
        if state != "REJECTED":
            kwargs = {
                "accepted_odds": Decimal("2"),
                "accepted_stake": Decimal(
                    "2" if state == "PARTIAL" else stake
                ),
            }
        ledger.acknowledge(
            ExternalAcknowledgement(
                attempt_id="attempt-1",
                external_receipt_id="bet-1",
                status=status,
                acknowledged_at="2026-09-21T23:59:00+00:00",
                **kwargs,
            )
        )
    elif state != "SUBMITTED":  # pragma: no cover
        raise AssertionError(state)
    return ledger, bound, provider_ref


def _current(
    provider_ref,
    *,
    bet_id="bet-1",
    side="BACK",
    price=2,
    size=5,
    matched=2,
    remaining=3,
    average=2,
    ref=None,
    status="EXECUTABLE",
):
    return {
        "betId": bet_id,
        "marketId": "1.234",
        "selectionId": 10,
        "side": side,
        "status": status,
        "placedDate": "2026-09-21T23:58:30+00:00",
        "priceSize": {"price": price, "size": size},
        "averagePriceMatched": average,
        "sizeMatched": matched,
        "sizeRemaining": remaining,
        "customerOrderRef": provider_ref if ref is None else ref,
    }


def _cleared(provider_ref, *, status="SETTLED", **changes):
    row = {
        "betId": "bet-1",
        "marketId": "1.234",
        "eventId": "event-1",
        "selectionId": 10,
        "side": "BACK",
        "placedDate": "2026-09-21T23:58:30+00:00",
        "settledDate": "2026-09-22T00:00:00+00:00",
        "priceRequested": 2,
        "priceMatched": 2,
        "sizeSettled": 2,
        "profit": 1,
        "customerOrderRef": provider_ref,
    }
    row.update(changes)
    return status, row


def _capture(provider_ref, *, current=(), cleared=None):
    by_status = {}
    if cleared:
        by_status[cleared[0]] = [cleared[1]]
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=_Transport(current, by_status),
        clock=lambda: datetime(2026, 9, 22, tzinfo=timezone.utc),
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


def test_partial_ack_uses_matched_plus_remaining_not_matched_only(tmp_path):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, matched=2, remaining=3)]),
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.EXACT
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.CURRENT_ORDER
    assert evidence.capital_at_risk == Decimal("5")
    assert evidence.capital_at_risk != Decimal("2")
    assert evidence.execution_authority is False
    assert evidence.readback_observed_at == "2026-09-22T00:00:00+00:00"
    assert evidence.provider_row_observed_at == "2026-09-22T00:00:00+00:00"
    assert len(evidence.readback_request_scope_sha256) == 64
    evidence.assert_authoritative()


def test_current_order_releases_only_provider_proven_dead_remainder(tmp_path):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, matched=2, remaining=1)]),
    )
    assert evidence.capital_at_risk == Decimal("3")


def test_stale_current_order_cannot_issue_exact_live_risk(tmp_path, monkeypatch):
    monkeypatch.setattr(
        live_risk,
        "_utc_now",
        lambda: datetime(2026, 9, 22, 0, 0, 31, tzinfo=timezone.utc),
    )
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, matched=2, remaining=3)]),
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.UNKNOWN
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.CURRENT_ORDER_NOT_CURRENT
    assert evidence.capital_at_risk is None
    assert evidence.provider_row_observed_at == "2026-09-22T00:00:00+00:00"


def test_future_current_order_cannot_issue_exact_live_risk(tmp_path, monkeypatch):
    monkeypatch.setattr(
        live_risk,
        "_utc_now",
        lambda: datetime(2026, 9, 21, 23, 59, 58, tzinfo=timezone.utc),
    )
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, matched=2, remaining=3)]),
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.UNKNOWN
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.CURRENT_ORDER_NOT_CURRENT
    assert evidence.capital_at_risk is None


@pytest.mark.parametrize(
    "checked_at",
    [
        datetime(2026, 9, 22, 0, 0, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 21, 23, 59, 59, tzinfo=timezone.utc),
    ],
)
def test_current_order_freshness_boundaries_remain_exact(
    tmp_path, monkeypatch, checked_at
):
    monkeypatch.setattr(live_risk, "_utc_now", lambda: checked_at)
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, matched=2, remaining=3)]),
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.EXACT
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.CURRENT_ORDER
    evidence.assert_authoritative()


def test_exact_current_order_authority_expires_at_use(tmp_path, monkeypatch):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, matched=2, remaining=3)]),
    )
    evidence.assert_authoritative()
    monkeypatch.setattr(
        live_risk,
        "_utc_now",
        lambda: datetime(2026, 9, 22, 0, 0, 31, tzinfo=timezone.utc),
    )
    with pytest.raises(
        BetfairLiveCapitalAtRiskError,
        match="current-order evidence is no longer current",
    ):
        evidence.assert_authoritative()


@pytest.mark.parametrize("state", ["SUBMITTED", "UNKNOWN"])
def test_positive_current_risk_can_precede_terminal_ledger_state(tmp_path, state):
    ledger, bound, ref = _context(tmp_path, state)
    evidence = _resolve(
        ledger,
        bound,
        _capture(
            ref,
            current=[_current(ref, matched=0, remaining=5, average=0)],
        ),
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.EXACT
    assert evidence.capital_at_risk == Decimal("5")


def test_cleared_terminal_releases_live_risk_after_durable_partial(tmp_path):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger, bound, _capture(ref, cleared=_cleared(ref))
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.EXACT
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.CLEARED_TERMINAL
    assert evidence.capital_at_risk == Decimal("0")


def test_voided_cleared_order_can_release_live_risk_after_durable_partial(tmp_path):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, cleared=_cleared(ref, status="VOIDED", profit=0)),
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.EXACT
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.CLEARED_TERMINAL
    assert evidence.capital_at_risk == Decimal("0")


@pytest.mark.parametrize("status", ["CANCELLED", "LAPSED"])
def test_cancelled_or_lapsed_cleared_order_does_not_release_matched_exposure(
    tmp_path, status
):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, cleared=_cleared(ref, status=status)),
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.UNKNOWN
    assert (
        evidence.reason
        is BetfairLiveCapitalAtRiskReason.CLEARED_MATCHED_EXPOSURE_UNRESOLVED
    )
    assert evidence.capital_at_risk is None


@pytest.mark.parametrize("state", ["SUBMITTED", "UNKNOWN"])
def test_cleared_cannot_release_before_durable_reconciliation(tmp_path, state):
    ledger, bound, ref = _context(tmp_path, state)
    evidence = _resolve(
        ledger, bound, _capture(ref, cleared=_cleared(ref))
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.UNKNOWN
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.DURABLE_STATE_CONFLICT
    assert evidence.capital_at_risk is None


def test_absence_is_unknown_not_zero(tmp_path):
    ledger, bound, ref = _context(tmp_path, "UNKNOWN")
    evidence = _resolve(ledger, bound, _capture(ref))
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.UNKNOWN
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.NO_PROVIDER_ROW


def test_current_plus_cleared_is_cross_call_revision_unknown(tmp_path):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(
            ref,
            current=[_current(ref)],
            cleared=_cleared(ref),
        ),
    )
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.CROSS_CALL_REVISION


def test_multiple_provider_rows_are_unknown(tmp_path):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(
            ref,
            current=[
                _current(ref, bet_id="bet-1"),
                _current(ref, bet_id="bet-2"),
            ],
        ),
    )
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.MULTIPLE_PROVIDER_ROWS


def test_foreign_customer_order_ref_fails_closed(tmp_path):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, ref="f" * 32)]),
    )
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.IDENTITY_MISMATCH


def test_durable_receipt_identity_must_match_provider_bet_id(tmp_path):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, bet_id="bet-other")]),
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.UNKNOWN
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.IDENTITY_MISMATCH


def test_exact_live_stake_addition_ignores_ambient_decimal_context():
    with localcontext() as context:
        context.prec = 3
        assert _exact_add(
            Decimal("0.12345"),
            Decimal("0.12345"),
        ) == Decimal("0.24690")


@pytest.mark.parametrize(
    "changes",
    [
        {"price": 2.02},
        {"size": 6},
        {"matched": 3, "remaining": 3},
        {"matched": 2, "remaining": 3, "average": 0},
        {"matched": 0, "remaining": 0, "average": 0},
        {"status": "EXECUTABLE", "matched": 2, "remaining": 0},
        {"status": "EXECUTION_COMPLETE", "matched": 2, "remaining": 3},
        {"status": "MYSTERY"},
    ],
)
def test_current_economic_inconsistency_is_unknown(tmp_path, changes):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, **changes)]),
    )
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.ECONOMIC_INCONSISTENCY
    assert evidence.capital_at_risk is None


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "EXECUTABLE", "matched": 2, "remaining": 3},
        {"status": "EXECUTION_COMPLETE", "matched": 5, "remaining": 0},
    ],
)
def test_current_status_remaining_coherence_valid_controls_are_exact(tmp_path, changes):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, **changes)]),
    )
    assert evidence.truth is BetfairLiveCapitalAtRiskTruth.EXACT
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.CURRENT_ORDER


def test_rejected_ledger_conflicts_with_live_provider_row(tmp_path):
    ledger, bound, ref = _context(tmp_path, "REJECTED")
    evidence = _resolve(
        ledger, bound, _capture(ref, current=[_current(ref)])
    )
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.DURABLE_STATE_CONFLICT


def test_lay_remains_unknown_until_execution_semantics_widen(tmp_path):
    ledger, bound, ref = _context(tmp_path, "SUBMITTED", side="LAY")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, side="LAY")]),
    )
    assert evidence.reason is BetfairLiveCapitalAtRiskReason.UNSUPPORTED_ACTION_SEMANTICS


def test_copied_readback_cannot_mint_authority(tmp_path):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    capture = _capture(ref, current=[_current(ref)])
    with pytest.raises(
        BetfairLiveCapitalAtRiskError, match="canonical provider evidence"
    ):
        _resolve(ledger, bound, replace(capture))


def test_copied_result_cannot_mint_authority(tmp_path):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger, bound, _capture(ref, current=[_current(ref)])
    )
    evidence.assert_authoritative()
    with pytest.raises(BetfairLiveCapitalAtRiskError, match="not issued"):
        replace(evidence).assert_authoritative()

def test_class_method_rebinding_cannot_authorize_caller_exact_risk(monkeypatch):
    """Public class-method rebinding cannot bypass the closure-owned issuer registry."""

    forged = live_risk.BetfairLiveCapitalAtRiskEvidence(
        truth=BetfairLiveCapitalAtRiskTruth.EXACT,
        reason=BetfairLiveCapitalAtRiskReason.CURRENT_ORDER,
        capital_at_risk=Decimal("500.00"),
        plan_id="caller-plan",
        attempt_id="caller-attempt",
        attempt_state=live_risk.AttemptState.SUBMITTED,
        action_id="caller-action",
        provider_order_ref="caller-provider-ref",
        bet_id=None,
        readback_observed_at="2026-09-22T14:49:59Z",
        provider_row_observed_at="2026-09-22T14:49:59Z",
        readback_request_scope_sha256="1" * 64,
        readback_evidence_sha256="2" * 64,
        ledger_snapshot_sha256="3" * 64,
    )

    monkeypatch.setattr(
        live_risk.BetfairLiveCapitalAtRiskEvidence,
        "assert_authoritative",
        lambda self: None,
    )

    with pytest.raises(BetfairLiveCapitalAtRiskError, match="not issued"):
        forged.assert_authoritative()


def test_class_method_rebinding_keeps_canonical_evidence_authoritative(
    tmp_path, monkeypatch
):
    ledger, bound, ref = _context(tmp_path, "PARTIAL")
    evidence = _resolve(
        ledger,
        bound,
        _capture(ref, current=[_current(ref, matched=2, remaining=3)]),
    )

    monkeypatch.setattr(
        live_risk.BetfairLiveCapitalAtRiskEvidence,
        "assert_authoritative",
        lambda self: None,
    )

    evidence.assert_authoritative()

