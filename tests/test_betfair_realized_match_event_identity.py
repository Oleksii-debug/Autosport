from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_realized_match import (
    RealizedMatchEvidenceError,
    RealizedMatchSource,
    resolve_betfair_realized_match,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


NOW = datetime(2026, 9, 21, 10, 31, tzinfo=timezone.utc)
MARKET_ID = "1.234"
EVENT_ID = "event-1"
SELECTION_ID = 10
ATTEMPT_ID = "attempt-event-identity"


class _Transport:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)

    def post(
        self,
        _url: str,
        *,
        headers: object,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        del headers, body, timeout_seconds
        if not self._responses:
            raise AssertionError("unexpected Betfair readback call")
        return self._responses.pop(0)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _prepared(root: Path) -> tuple[ExecutionPlan, RealExecutionLedger, str]:
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
    provider_order_ref = ledger.bind_provider_order_reference(
        attempt_id=ATTEMPT_ID,
        provider_id="betfair",
    )
    ledger.mark_submitted(
        ATTEMPT_ID,
        submitted_at="2026-09-21T09:54:55+00:00",
    )
    return plan, ledger, provider_order_ref


def _cleared_row(
    provider_order_ref: str,
    *,
    event_id: str | None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "betId": "bet-1",
        "marketId": MARKET_ID,
        "selectionId": SELECTION_ID,
        "side": "BACK",
        "placedDate": "2026-09-21T09:54:55+00:00",
        "settledDate": "2026-09-21T10:30:00+00:00",
        "priceRequested": 3.0,
        "priceMatched": 3.2,
        "sizeSettled": 10.0,
        "profit": 22.0,
        "customerOrderRef": provider_order_ref,
    }
    if event_id is not None:
        row["eventId"] = event_id
    return row


def _capture(provider_order_ref: str, *, cleared_event_id: str | None):
    responses = [
        _response(
            [{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}],
            1,
        ),
        _response({"currentOrders": [], "moreAvailable": False}, 2),
        _response(
            {
                "clearedOrders": [
                    _cleared_row(
                        provider_order_ref,
                        event_id=cleared_event_id,
                    )
                ],
                "moreAvailable": False,
            },
            3,
        ),
        _response({"clearedOrders": [], "moreAvailable": False}, 4),
        _response({"clearedOrders": [], "moreAvailable": False}, 5),
        _response({"clearedOrders": [], "moreAvailable": False}, 6),
    ]
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=_Transport(responses),
        clock=lambda: NOW,
        venue_id="betfair",
        account_id="acct-1",
    ).read_execution_readback(
        action_id="action-1",
        provider_order_ref=provider_order_ref,
        market_id=MARKET_ID,
    )


def test_cleared_row_foreign_event_id_fails_closed(tmp_path: Path) -> None:
    plan, ledger, provider_order_ref = _prepared(tmp_path)
    readback = _capture(
        provider_order_ref,
        cleared_event_id="event-foreign",
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="cleared provider order row belongs to a different event",
    ):
        resolve_betfair_realized_match(
            plan,
            ledger,
            readback,
            attempt_id=ATTEMPT_ID,
        )


def test_cleared_row_without_event_id_uses_catalogue_event_identity(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_order_ref = _prepared(tmp_path)
    evidence = resolve_betfair_realized_match(
        plan,
        ledger,
        _capture(provider_order_ref, cleared_event_id=None),
        attempt_id=ATTEMPT_ID,
    )

    evidence.assert_authoritative()
    assert evidence.source is RealizedMatchSource.CLEARED_BET
    assert evidence.event_id == EVENT_ID
    assert evidence.bet_id == "bet-1"
    assert evidence.finalized is True
