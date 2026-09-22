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



@pytest.fixture(autouse=True)
def _isolated_monotonic_authority(tmp_path, monkeypatch) -> None:
    authority_root = tmp_path.parent / f"{tmp_path.name}-monotonic-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
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
        self.catalog_event_id = "event-1"
        self.cleared_event_id = "event-1"
        self.cleared_market_id = "1.234"
        self.cleared_selection_id = 10
        self.cleared_side = "BACK"
        self.customer_order_ref: str | None = None
        self.extra_exact_ref_selection_id: int | None = None

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        request = json.loads(body.decode("utf-8"))
        method = request["method"]
        request_id = request["id"]
        if method.endswith("listMarketCatalogue"):
            requested_market_id = request["params"]["filter"]["marketIds"][0]
            result = [{
                "marketId": requested_market_id,
                "event": {"id": self.catalog_event_id},
            }]
        elif method.endswith("listCurrentOrders"):
            result = {"currentOrders": [], "moreAvailable": False}
        elif method.endswith("listClearedOrders"):
            rows = []
            if request["params"]["betStatus"] == self.provider_status:
                rows.append({
                    "betId": "bet-777",
                    "marketId": self.cleared_market_id,
                    "eventId": self.cleared_event_id,
                    "selectionId": self.cleared_selection_id,
                    "side": self.cleared_side,
                    "placedDate": "2026-09-21T18:00:00+00:00",
                    "settledDate": self.settled_date,
                    "priceRequested": 2,
                    "priceMatched": 2,
                    "sizeSettled": 5,
                    "profit": self.profit,
                    "customerOrderRef": self.customer_order_ref,
                })
                if self.extra_exact_ref_selection_id is not None:
                    rows.append({
                        "betId": "bet-conflict",
                        "marketId": self.cleared_market_id,
                        "eventId": self.cleared_event_id,
                        "selectionId": self.extra_exact_ref_selection_id,
                        "side": self.cleared_side,
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


def test_superseded_semantic_revision_cannot_reappear_as_new_current_truth(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")

    first = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision
    transport.provider_status = "VOIDED"
    transport.profit = 0
    transport.settled_date = "2026-09-21T19:30:00+00:00"
    corrected = _ingest(
        store,
        ledger,
        plan,
        action,
        _capture(client, provider_ref),
    ).revision

    transport.provider_status = "SETTLED"
    transport.profit = 4
    transport.settled_date = "2026-09-21T19:00:00+00:00"
    with pytest.raises(
        BetfairSettlementRevisionError,
        match="superseded semantic revision",
    ):
        _ingest(store, ledger, plan, action, _capture(client, provider_ref))

    assert len(store.revisions) == 2
    assert corrected.previous_revision_id == first.revision_id
    assert store.current("betfair", "acct-1", "bet-777") == corrected


def test_changed_content_at_equal_provider_settled_time_appends_observed_revision(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")

    first = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision
    transport.profit = -1
    corrected = _ingest(
        store,
        ledger,
        plan,
        action,
        _capture(client, provider_ref),
    ).revision

    assert corrected.revision_number == 2
    assert corrected.previous_revision_id == first.revision_id
    assert corrected.settled_date == first.settled_date
    assert corrected.provider_profit == Decimal("-1")
    assert store.current("betfair", "acct-1", "bet-777") == corrected


def test_changed_content_with_older_provider_settled_time_fails_closed(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")

    first = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision
    transport.profit = -1
    transport.settled_date = "2026-09-21T18:59:59+00:00"

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="regressed provider settled_date",
    ):
        _ingest(store, ledger, plan, action, _capture(client, provider_ref))

    assert store.revisions == (first,)
    assert store.current("betfair", "acct-1", "bet-777") == first


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


def test_caller_action_cannot_substitute_durable_plan_identity(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")
    forged = replace(
        action,
        event_id="event-forged",
        market_id="9.999",
        selection_id="11",
        side="LAY",
        quote_id="quote-forged",
        requested_odds=Decimal("3.25"),
        requested_stake=Decimal("7"),
    )
    transport.catalog_event_id = forged.event_id
    transport.cleared_event_id = forged.event_id
    transport.cleared_market_id = forged.market_id
    transport.cleared_selection_id = int(forged.selection_id)
    transport.cleared_side = forged.side

    capture = client.read_execution_readback(
        action_id=forged.action_id,
        market_id=forged.market_id,
        provider_order_ref=provider_ref,
    )
    capture.assert_authoritative()

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="differs from durable execution plan",
    ):
        _ingest(store, ledger, plan, forged, capture)

    assert store.revisions == ()


def test_matching_cleared_row_with_contradictory_event_fails_closed(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")
    transport.cleared_event_id = "event-other"

    capture = _capture(client, provider_ref)
    assert capture.market_event.event_id == action.event_id
    matching_order = next(
        order
        for _status, pages in capture.cleared_pages_by_status
        for page in pages
        for order in page.orders
        if order.bet_id == "bet-777"
    )
    assert matching_order.event_id == "event-other"

    with pytest.raises(BetfairSettlementRevisionError, match="cleared row event mismatch"):
        _ingest(store, ledger, plan, action, capture)

    assert store.revisions == ()


def test_failed_reload_never_exposes_validated_prefix_as_current_truth(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    path = tmp_path / "settlement.jsonl"
    store = BetfairSettlementRevisionStore(path)

    first = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision
    transport.provider_status = "VOIDED"
    transport.profit = 0
    transport.settled_date = "2026-09-21T19:30:00+00:00"
    second = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision
    assert store.revisions == (first, second)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    corrupted = json.loads(lines[1])
    corrupted["revision"]["provider_profit"] = "999"
    path.write_text(lines[0] + "\n" + json.dumps(corrupted, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(BetfairSettlementRevisionError, match="digest mismatch"):
        store._reload()

    assert store.revisions == (first, second)
    assert store.current("betfair", "acct-1", "bet-777") == second
    assert store.as_of("betfair", "acct-1", "bet-777", first.available_at) == first


def test_exact_provider_ref_with_contradictory_selection_fails_closed(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")
    transport.extra_exact_ref_selection_id = 11

    capture = _capture(client, provider_ref)
    assert sum(
        order.customer_order_ref == provider_ref
        for _status, pages in capture.cleared_pages_by_status
        for page in pages
        for order in page.orders
    ) == 2

    with pytest.raises(
        BetfairSettlementRevisionError,
        match="provider order reference identity mismatch",
    ):
        _ingest(store, ledger, plan, action, capture)

    assert store.revisions == ()


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

    # A hard-killed legacy writer may leave only the lock path behind. File
    # existence is not ownership: restart must recover without manual cleanup.
    clean_store._writer_lock_path.write_text(
        "stale-from-crashed-process",
        encoding="utf-8",
    )
    restarted = BetfairSettlementRevisionStore(clean_path)
    capture = _capture(client, provider_ref)
    recovered = _ingest(restarted, ledger, plan, action, capture)
    assert recovered.created is True
    assert restarted._writer_lock_path.exists()

    # A genuinely live competing owner still fails closed immediately.
    with restarted._writer_lock():
        with pytest.raises(
            BetfairSettlementBusyError,
            match="writer lock is held by another process",
        ):
            _ingest(restarted, ledger, plan, action, capture)


def test_complete_valid_tail_rollback_is_rejected_by_independent_authority(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    path = tmp_path / "settlement.jsonl"
    store = BetfairSettlementRevisionStore(path)

    first = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision
    first_bytes = path.read_bytes()

    transport.provider_status = "VOIDED"
    transport.profit = 0
    transport.settled_date = "2026-09-21T19:30:00+00:00"
    second = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision
    assert second.revision_number == 2
    assert BetfairSettlementRevisionStore(path).current(
        "betfair", "acct-1", "bet-777"
    ) == second

    # The restored file is internally valid and hash-chain complete, but stale.
    path.write_bytes(first_bytes)

    with pytest.raises(BetfairSettlementRevisionError, match="monotonic authority"):
        BetfairSettlementRevisionStore(path)

    assert first.revision_number == 1


def test_deleted_local_journal_cannot_rebootstrap_after_committed_history(tmp_path) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    path = tmp_path / "settlement.jsonl"
    store = BetfairSettlementRevisionStore(path)
    _ingest(store, ledger, plan, action, _capture(client, provider_ref))

    path.unlink()

    with pytest.raises(BetfairSettlementRevisionError, match="monotonic authority"):
        BetfairSettlementRevisionStore(path)


def test_prepare_without_local_append_is_aborted_and_retry_remains_available(
    tmp_path, monkeypatch
) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    path = tmp_path / "settlement.jsonl"
    store = BetfairSettlementRevisionStore(path)
    first = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision

    transport.provider_status = "VOIDED"
    transport.profit = 0
    transport.settled_date = "2026-09-21T19:30:00+00:00"

    def interrupt_before_append(revision) -> None:
        raise RuntimeError("injected before local append")

    monkeypatch.setattr(store, "_append", interrupt_before_append)
    with pytest.raises(RuntimeError, match="injected before local append"):
        _ingest(store, ledger, plan, action, _capture(client, provider_ref))
    monkeypatch.undo()

    restarted = BetfairSettlementRevisionStore(path)
    assert restarted.current("betfair", "acct-1", "bet-777") == first

    second = _ingest(
        restarted, ledger, plan, action, _capture(client, provider_ref)
    ).revision
    assert second.revision_number == 2
    assert second.provider_status == "VOIDED"


def test_concurrent_constructor_cannot_abort_active_monotonic_prepare(
    tmp_path, monkeypatch
) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    path = tmp_path / "settlement.jsonl"
    store = BetfairSettlementRevisionStore(path)
    _ingest(store, ledger, plan, action, _capture(client, provider_ref))

    transport.provider_status = "VOIDED"
    transport.profit = 0
    transport.settled_date = "2026-09-21T19:30:00+00:00"
    durable_append = store._append

    def competing_open_then_append(revision) -> None:
        # ingest already holds the settlement OS writer lock and has published
        # monotonic PREPARE. A read/startup opener must not recover that PREPARE
        # against the still-old journal and abort the active writer.
        with pytest.raises(
            BetfairSettlementBusyError,
            match="writer lock is held by another process",
        ):
            BetfairSettlementRevisionStore(path)
        durable_append(revision)

    monkeypatch.setattr(store, "_append", competing_open_then_append)
    second = _ingest(
        store, ledger, plan, action, _capture(client, provider_ref)
    ).revision
    monkeypatch.undo()

    assert second.revision_number == 2
    assert second.provider_status == "VOIDED"

    restarted = BetfairSettlementRevisionStore(path)
    assert restarted.current("betfair", "acct-1", "bet-777") == second


def test_local_append_before_monotonic_commit_recovers_exact_intended_tail(
    tmp_path, monkeypatch
) -> None:
    transport = _Transport()
    ledger, plan, action, client, provider_ref = _accepted_context(tmp_path, transport)
    path = tmp_path / "settlement.jsonl"
    store = BetfairSettlementRevisionStore(path)
    first = _ingest(store, ledger, plan, action, _capture(client, provider_ref)).revision

    transport.provider_status = "VOIDED"
    transport.profit = 0
    transport.settled_date = "2026-09-21T19:30:00+00:00"
    durable_append = store._append

    def append_then_interrupt(revision) -> None:
        durable_append(revision)
        raise RuntimeError("injected after local append")

    monkeypatch.setattr(store, "_append", append_then_interrupt)
    with pytest.raises(RuntimeError, match="injected after local append"):
        _ingest(store, ledger, plan, action, _capture(client, provider_ref))
    monkeypatch.undo()

    # The process-local store never publishes the uncommitted successor.
    assert store.current("betfair", "acct-1", "bet-777") == first

    # Restart sees the exact PREPARE target on disk and resolves it to COMMIT.
    restarted = BetfairSettlementRevisionStore(path)
    second = restarted.current("betfair", "acct-1", "bet-777")
    assert second is not None
    assert second.revision_number == 2
    assert second.provider_status == "VOIDED"
    assert second.previous_revision_id == first.revision_id