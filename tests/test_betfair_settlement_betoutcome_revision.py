from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_settlement_revisions import BetfairSettlementRevisionStore
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
        self.bet_outcome = "WON"
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
            if request["params"]["betStatus"] == "SETTLED":
                rows.append(
                    {
                        "betId": "bet-777",
                        "marketId": "1.234",
                        "eventId": "event-1",
                        "selectionId": 10,
                        "side": "BACK",
                        "placedDate": "2026-09-21T18:00:00+00:00",
                        "settledDate": "2026-09-21T19:00:00+00:00",
                        "priceRequested": 2,
                        "priceMatched": 2,
                        "sizeSettled": 5,
                        "profit": 4,
                        "customerOrderRef": self.customer_order_ref,
                        "betOutcome": self.bet_outcome,
                    }
                )
            result = {"clearedOrders": rows, "moreAvailable": False}
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _context(tmp_path):
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="10",
        side="BACK",
        requested_odds=Decimal("2"),
        requested_stake=Decimal("5"),
        quote_id="quote-1",
        quote_observed_at="2026-09-21T17:59:00+00:00",
        expires_at="2026-09-21T18:05:00+00:00",
    )
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
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id="attempt-1",
        provider_id="betfair",
    )
    ledger.mark_submitted(
        "attempt-1",
        submitted_at="2026-09-21T18:01:00+00:00",
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-1",
            external_receipt_id="bet-777",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at="2026-09-21T18:02:00+00:00",
            accepted_odds=Decimal("2"),
            accepted_stake=Decimal("5"),
        )
    )
    transport = _Transport()
    transport.customer_order_ref = provider_ref
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=_Clock(),
        venue_id="betfair",
        account_id="acct-1",
    )
    return ledger, plan, action, provider_ref, transport, client


def _capture(client: BetfairReadOnlyClient, provider_ref: str):
    return client.read_execution_readback(
        action_id="action-1",
        market_id="1.234",
        provider_order_ref=provider_ref,
    )


def test_bet_outcome_only_provider_correction_creates_new_revision(tmp_path) -> None:
    ledger, plan, action, provider_ref, transport, client = _context(tmp_path)
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")

    first_capture = _capture(client, provider_ref)
    first = store.ingest(
        ledger,
        plan_id=plan.plan_id,
        attempt_id="attempt-1",
        action=action,
        capture=first_capture,
    )

    transport.bet_outcome = "LOST"
    second_capture = _capture(client, provider_ref)
    assert second_capture.evidence_sha256 != first_capture.evidence_sha256

    second = store.ingest(
        ledger,
        plan_id=plan.plan_id,
        attempt_id="attempt-1",
        action=action,
        capture=second_capture,
    )

    assert second.created is True
    assert second.revision.revision_number == 2
    assert second.revision.previous_revision_id == first.revision.revision_id
    assert second.revision.revision_id != first.revision.revision_id
