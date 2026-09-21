from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport.betfair_account_readonly import BetfairReadOnlyClient, BetfairSessionCredentials
from autosport.betfair_settlement_revisions import (
    BetfairSettlementBusyError,
    BetfairSettlementRevisionError,
    BetfairSettlementRevisionStore,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(seconds=1)
        return result


class _Transport:
    def __init__(self) -> None:
        self.provider_status = "SETTLED"
        self.profit = 4
        self.settled_date = "2026-09-21T19:00:00+00:00"
        self.customer_order_ref: str | None = None

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        request = json.loads(body.decode("utf-8"))
        method = request["method"]
        request_id = request["id"]
        if method.endswith("listMarketCatalogue"):
            result = [{"marketId": "1.234", "event": {"id": "event-1"}}]
        elif method.endswith("listCurrentOrders"):
            result = {"currentOrders": [], "moreAvailable": False}
        elif method.endswith("listClearedOrders"):
            rows = []
            if request["params"]["betStatus"] == self.provider_status:
                rows.append({
                    "betId": "bet-777",
                    "marketId": "1.234",
                    "eventId": "event-1",
                    "selectionId": 10,
                    "side": "BACK",
                    "placedDate": "2026-09-21T18:00:00+00:00",
                    "settledDate": self.settled_date,
                    "priceRequested": 2,
                    "priceMatched": 2,
                    "sizeSettled": 5,
                    "profit": self.profit,
                    "customerOrderRef": self.customer_order_ref,
                })
            result = {"clearedOrders": rows, "moreAvailable": False}
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, separators=(",", ":")).encode()


def _action(**changes) -> ExecutionAction:
    values = {
        "action_id": "action-1",
        "bookmaker_id": "betfair",
        "account_id": "acct-1",
        "event_id": "event-1",
        "market_id": "1.234",
        "selection_id": "10",
        "side": "BACK",
        "requested_odds": Decimal("2"),
        "requested_stake": Decimal("5"),
        "quote_id": "quote-1",
        "quote_observed_at": "2026-09-21T17:59:00+00:00",
        "expires_at": "2026-09-21T18:05:00+00:00",
    }
    values.update(changes)
    return ExecutionAction(**values)


def _accepted_context(tmp_path, transport: _Transport):
    action = _action()
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-21T17:59:30+00:00",
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at="2026-09-21T18:00:00+00:00",
    )
    provider_ref = ledger.bind_provider_order_reference(attempt_id="attempt-1", provider_id="betfair")
    transport.customer_order_ref = provider_ref
    ledger.mark_submitted("attempt-1", submitted_at="2026-09-21T18:01:00+00:00")
    ledger.acknowledge(ExternalAcknowledgement(
        attempt_id="attempt-1",
        external_receipt_id="bet-777",
        status=AcknowledgementStatus.ACCEPTED,
        acknowledged_at="2026-09-21T18:02:00+00:00",
        accepted_odds=Decimal("2"),
        accepted_stake=Decimal("5"),
    ))
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=_Clock(),
        venue_id="betfair",
        account_id="acct-1",
    )
    return ledger, plan, action, client, provider_ref


def _capture(client: BetfairReadOnlyClient, provider_ref: str):
    return client.read_execution_readback(
        action_id="action-1", market_id="1.234", provider_order_ref=provider_ref
    )


def _ingest(store, ledger, plan, action, capture):
    return store.ingest(
        ledger,
        plan_id=plan.plan_id,
        attempt_id="attempt-1",
        action=action,
        capture=capture,
    )


def test_identical_reread_is_idempotent_and_restart_safe(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    path = tmp_path / "settlement.jsonl"
    store = BetfairSettlementRevisionStore(path)

    first = _ingest(store, ledger, plan, action, _capture(client, provider_ref))
    second = _ingest(store, ledger, plan, action, _capture(client, provider_ref))

    assert first.created is True
    assert second.created is False
    assert second.revision.revision_id == first.revision.revision_id
    assert first.revision.plan_id == "plan-1"
    assert first.revision.attempt_id == "attempt-1"
    assert first.revision.external_bet_id == "bet-777"
    assert first.revision.permanent_final is False
    assert first.revision.terminal_space_exact is False
    assert len(store.revisions) == 1

    restarted = BetfairSettlementRevisionStore(path)
    assert restarted.current("betfair", "acct-1", "bet-777") == first.revision


def test_later_void_correction_appends_without_backward_leakage(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")

    first = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision
    transport.provider_status = "VOIDED"
    transport.profit = 0
    transport.settled_date = "2026-09-21T19:30:00+00:00"
    second = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision

    assert second.revision_number == 2
    assert second.previous_revision_id == first.revision_id
    assert second.provider_status == "VOIDED"
    assert second.provider_profit == Decimal("0")
    assert second.permanent_final is False
    assert second.terminal_space_exact is False
    assert store.as_of("betfair", "acct-1", "bet-777", first.available_at) == first
    assert store.as_of("betfair", "acct-1", "bet-777", second.available_at) == second


def test_forged_or_mismatched_capture_fails_closed(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")
    capture = _capture(client, provider_ref)

    with pytest.raises(BetfairSettlementRevisionError, match="event mismatch"):
        _ingest(store, ledger, plan, replace(action, event_id="other-event"), capture)

    forged = replace(capture, evidence_sha256="0" * 64)
    with pytest.raises(BetfairSettlementRevisionError, match="not canonical adapter-issued"):
        _ingest(store, ledger, plan, action, forged)


def test_requires_durable_attempt_receipt_owner_and_provider_ref(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")
    capture = _capture(client, provider_ref)

    with pytest.raises(BetfairSettlementRevisionError, match="ACCEPTED/PARTIAL"):
        store.ingest(
            ledger,
            plan_id=plan.plan_id,
            attempt_id="missing-attempt",
            action=action,
            capture=capture,
        )

    unbound_capture = client.read_execution_readback(action_id="action-1", market_id="1.234")
    with pytest.raises(BetfairSettlementRevisionError, match="customer_order_ref"):
        _ingest(store, ledger, plan, action, unbound_capture)


def test_tampered_log_and_parallel_writer_lock_fail_closed(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    path = tmp_path / "settlement.jsonl"
    store = BetfairSettlementRevisionStore(path)
    _ingest(store, ledger, plan, action, _capture(client, provider_ref))

    record = json.loads(path.read_text(encoding="utf-8"))
    record["revision"]["provider_profit"] = "999"
    path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(BetfairSettlementRevisionError, match="digest mismatch"):
        BetfairSettlementRevisionStore(path)

    clean_path = tmp_path / "clean-settlement.jsonl"
    clean_store = BetfairSettlementRevisionStore(clean_path)
    clean_store._writer_lock_path.write_text("occupied", encoding="utf-8")
    with pytest.raises(BetfairSettlementBusyError, match="writer lock"):
        _ingest(clean_store, ledger, plan, action, _capture(client, provider_ref))
