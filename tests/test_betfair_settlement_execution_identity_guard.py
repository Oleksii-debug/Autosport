from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport import betfair_settlement_revisions as settlement
from autosport.betfair_account_readonly import (
    ADAPTER_ID as BETFAIR_ADAPTER_ID,
    ADAPTER_VERSION as BETFAIR_ADAPTER_VERSION,
)
from autosport.real_execution_ledger import ExecutionAction, RealExecutionLedger


def _action(**changes) -> ExecutionAction:
    values = {
        "action_id": "action-1",
        "bookmaker_id": "betfair",
        "account_id": "acct-1",
        "event_id": "event-1",
        "market_id": "1.234",
        "selection_id": "10",
        "side": "BACK",
        "requested_odds": Decimal("2.10"),
        "requested_stake": Decimal("5.00"),
        "quote_id": "quote-1",
        "quote_observed_at": "2026-09-25T18:00:00+00:00",
        "expires_at": "2026-09-25T18:05:00+00:00",
    }
    values.update(changes)
    return ExecutionAction(**values)


def _capture(
    action: ExecutionAction,
    *,
    placed_date: str = "2026-09-25T18:01:00+00:00",
    settled_date: str = "2026-09-25T19:00:00+00:00",
    price_requested: Decimal = Decimal("2.10"),
    size_settled: Decimal = Decimal("5.00"),
):
    provider_ref = "provider-ref-1"
    order = SimpleNamespace(
        bet_id="bet-1",
        market_id=action.market_id,
        event_id=action.event_id,
        selection_id=int(action.selection_id),
        side=action.side,
        bet_status="SETTLED",
        placed_date=placed_date,
        settled_date=settled_date,
        price_requested=price_requested,
        price_matched=Decimal("2.10"),
        size_settled=size_settled,
        profit=Decimal("5.50"),
        customer_order_ref=provider_ref,
        customer_strategy_ref=None,
        evidence=SimpleNamespace(source_payload_sha256="a" * 64),
    )
    page = SimpleNamespace(orders=(order,))
    capture = SimpleNamespace(
        adapter_id=BETFAIR_ADAPTER_ID,
        adapter_version=BETFAIR_ADAPTER_VERSION,
        venue_id=action.bookmaker_id,
        account_id=action.account_id,
        action_id=action.action_id,
        market_id=action.market_id,
        market_event=SimpleNamespace(event_id=action.event_id),
        provider_order_ref=provider_ref,
        cleared_pages_by_status=(("SETTLED", (page,)),),
    )
    return capture, order


def _require_owner(ledger: RealExecutionLedger) -> None:
    action = _action()
    capture, _ = _capture(action)
    settlement._require_attempt_receipt_owner(
        ledger,
        plan_id="plan-1",
        attempt_id="attempt-1",
        action=action,
        capture=capture,
        external_bet_id="bet-1",
    )


def test_exact_provider_row_remains_accepted_by_existing_match_authority() -> None:
    action = _action()
    capture, order = _capture(action)

    assert settlement._match_order(action, capture) is order


def test_provider_settlement_cannot_predate_provider_placement() -> None:
    action = _action()
    capture, _ = _capture(
        action,
        placed_date="2026-09-25T19:00:01+00:00",
        settled_date="2026-09-25T19:00:00+00:00",
    )

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="chronology predates order placement",
    ):
        settlement._match_order(action, capture)


def test_provider_requested_price_must_equal_durable_execution_action() -> None:
    action = _action()
    capture, _ = _capture(action, price_requested=Decimal("2.11"))

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="requested price differs from durable execution action",
    ):
        settlement._match_order(action, capture)


def test_provider_settled_size_cannot_exceed_durable_requested_stake() -> None:
    action = _action()
    capture, _ = _capture(action, size_settled=Decimal("5.01"))

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="size exceeds durable execution action requested stake",
    ):
        settlement._match_order(action, capture)


def test_partial_settlement_below_requested_stake_is_not_overconstrained() -> None:
    action = _action()
    capture, order = _capture(action, size_settled=Decimal("2.00"))

    # This guard proves only the upper bound available from the durable action.
    # PARTIAL acknowledgement-to-final-settlement equality is a separate authority
    # and must not be invented here.
    assert settlement._match_order(action, capture) is order


def test_ledger_subclass_cannot_mint_settlement_execution_authority(tmp_path) -> None:
    class ForgedLedger(RealExecutionLedger):
        def saga(self, plan_id):  # pragma: no cover - must never dispatch
            raise AssertionError(plan_id)

    ledger = ForgedLedger(tmp_path / "forged-execution.jsonl")

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="exact RealExecutionLedger",
    ):
        _require_owner(ledger)


def test_exact_ledger_instance_cannot_shadow_authority_methods(tmp_path) -> None:
    ledger = RealExecutionLedger(tmp_path / "shadowed-execution.jsonl")
    ledger.__dict__["_events"] = lambda: []

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="instance-shadowed: _events",
    ):
        _require_owner(ledger)


def test_ledger_class_dispatch_rebind_fails_before_owner_resolution(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    monkeypatch.setattr(RealExecutionLedger, "saga", lambda self, plan_id: None)

    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="execution identity dispatch changed",
    ):
        _require_owner(ledger)
