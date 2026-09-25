from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_realized_match import (
    RealizedMatchEvidenceError,
    resolve_betfair_realized_match,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


ATTEMPT_ID = "attempt-ledger-authority"
MARKET_ID = "1.234"
EVENT_ID = "event-1"
SELECTION_ID = 10
NOW = datetime(2026, 9, 21, 9, 55, tzinfo=timezone.utc)


class _Transport:
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


def _prepared(root: Path) -> tuple[ExecutionPlan, RealExecutionLedger, object]:
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id=EVENT_ID,
        market_id=MARKET_ID,
        selection_id=str(SELECTION_ID),
        side="BACK",
        requested_odds="3.0",
        requested_stake="10",
        quote_id="quote-1",
        quote_observed_at="2026-09-21T09:54:50+00:00",
        expires_at="2026-09-21T09:56:00+00:00",
    )
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
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id=ATTEMPT_ID,
        provider_id="betfair",
    )
    ledger.mark_submitted(
        ATTEMPT_ID,
        submitted_at="2026-09-21T09:54:55+00:00",
    )
    current = {
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
        "customerOrderRef": provider_ref,
    }
    responses = [
        _response([{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}], 1),
        _response({"currentOrders": [current], "moreAvailable": False}, 2),
    ]
    for request_id in range(3, 7):
        responses.append(
            _response({"clearedOrders": [], "moreAvailable": False}, request_id)
        )
    capture = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=_Transport(responses),
        clock=lambda: NOW,
        venue_id="betfair",
        account_id="acct-1",
    ).read_execution_readback(
        action_id="action-1",
        provider_order_ref=provider_ref,
        market_id=MARKET_ID,
    )
    return plan, ledger, capture


@pytest.mark.parametrize(
    "method_name",
    ["verified_snapshot", "saga", "provider_order_reference"],
)
def test_instance_rebound_ledger_read_surface_fails_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    plan, ledger, capture = _prepared(tmp_path)
    original = getattr(ledger, method_name)
    calls = 0

    def rebound(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, method_name, rebound)

    with pytest.raises((RealizedMatchEvidenceError, TypeError)):
        resolve_betfair_realized_match(
            plan,
            ledger,
            capture,
            attempt_id=ATTEMPT_ID,
        )

    assert calls == 0


def test_real_execution_ledger_subclass_cannot_supply_positive_authority(
    tmp_path: Path,
) -> None:
    plan, ledger, capture = _prepared(tmp_path)

    class DerivedLedger(RealExecutionLedger):
        pass

    derived = DerivedLedger(ledger.path)

    with pytest.raises((RealizedMatchEvidenceError, TypeError)):
        resolve_betfair_realized_match(
            plan,
            derived,
            capture,
            attempt_id=ATTEMPT_ID,
        )
