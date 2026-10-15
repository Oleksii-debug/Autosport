from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_realized_match import (
    RealizedMatchSource,
    resolve_betfair_realized_match,
    validate_betfair_realized_match_revision,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


MARKET_ID = "1.234"
EVENT_ID = "event-1"
SELECTION_ID = 10
ATTEMPT_ID = "attempt-1"
FIXED_NOW = datetime(2026, 9, 21, 9, 55, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        del url, headers, body, timeout_seconds
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _action(
    action_id: str,
    *,
    bookmaker_id: str = "betfair",
    account_id: str = "acct-1",
    event_id: str = EVENT_ID,
    market_id: str = MARKET_ID,
    selection_id: str = str(SELECTION_ID),
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=bookmaker_id,
        account_id=account_id,
        event_id=event_id,
        market_id=market_id,
        selection_id=selection_id,
        side="BACK",
        requested_odds="3.0",
        requested_stake="10",
        quote_id=f"quote-{action_id}",
        quote_observed_at="2026-09-21T09:54:50+00:00",
        expires_at="2026-09-21T09:56:00+00:00",
    )


def _prepared(root: Path) -> tuple[ExecutionPlan, RealExecutionLedger, str]:
    action = _action("action-1")
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-21T09:54:45+00:00",
        actions=(action,),
    )
    ledger = RealExecutionLedger(root / "real-execution.jsonl")
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id=ATTEMPT_ID,
        reserved_at="2026-09-21T09:54:52+00:00",
    )
    provider_order_ref = ledger.bind_provider_order_reference(
        attempt_id=ATTEMPT_ID,
        provider_id="betfair",
    )
    ledger.mark_submitted(
        ATTEMPT_ID,
        submitted_at="2026-09-21T09:54:55+00:00",
    )
    return plan, ledger, provider_order_ref


def _capture(provider_order_ref: str):
    current_order = {
        "betId": "bet-1",
        "marketId": MARKET_ID,
        "selectionId": SELECTION_ID,
        "side": "BACK",
        "status": "EXECUTABLE",
        "placedDate": "2026-09-21T09:54:55+00:00",
        "priceSize": {"price": 3.0, "size": 10.0},
        "averagePriceMatched": 3.2,
        "sizeMatched": 4.0,
        "sizeRemaining": 6.0,
        "customerOrderRef": provider_order_ref,
    }
    responses = [
        _response([{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}], 1),
        _response({"currentOrders": [current_order], "moreAvailable": False}, 2),
    ]
    for request_id in range(3, 7):
        responses.append(
            _response({"clearedOrders": [], "moreAvailable": False}, request_id)
        )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=FakeTransport(responses),
        clock=lambda: FIXED_NOW,
        venue_id="betfair",
        account_id="acct-1",
    )
    return client.read_execution_readback(
        action_id="action-1",
        provider_order_ref=provider_order_ref,
        market_id=MARKET_ID,
    )


def _append_unrelated_plan(ledger: RealExecutionLedger) -> None:
    unrelated = _action(
        "unrelated-action",
        bookmaker_id="book-b",
        account_id="acct-b",
        event_id="event-b",
        market_id="market-b",
        selection_id="selection-b",
    )
    ledger.reserve_plan(
        ExecutionPlan(
            plan_id="unrelated-plan",
            bookmaker_profile_version="unrelated-profile",
            decision_id="unrelated-decision",
            approval_id="unrelated-approval",
            created_at="2026-09-21T09:54:46+00:00",
            actions=(unrelated,),
        )
    )


def test_unrelated_ledger_append_does_not_rewrite_realized_match_identity(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_order_ref = _prepared(tmp_path)
    capture = _capture(provider_order_ref)
    before = resolve_betfair_realized_match(
        plan, ledger, capture, attempt_id=ATTEMPT_ID
    )
    before.assert_authoritative()
    assert before.source is RealizedMatchSource.CURRENT_ORDER

    _append_unrelated_plan(ledger)

    after = resolve_betfair_realized_match(
        plan, ledger, capture, attempt_id=ATTEMPT_ID
    )
    after.assert_authoritative()
    assert before.ledger_snapshot_sha256 != after.ledger_snapshot_sha256
    assert before.readback_evidence_sha256 == after.readback_evidence_sha256
    assert before.provider_order_ref == after.provider_order_ref
    assert before.provider_matched_stake == after.provider_matched_stake
    assert before.provider_matched_odds == after.provider_matched_odds
    assert after.evidence_id == before.evidence_id
    selected = validate_betfair_realized_match_revision(before, after)
    assert selected.evidence_id == before.evidence_id
