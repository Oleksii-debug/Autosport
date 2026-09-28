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
ATTEMPT_ID = "attempt-current-status"


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


def _current_row(
    provider_order_ref: str,
    *,
    status: str,
    size_matched: float,
    size_remaining: float,
) -> dict[str, object]:
    return {
        "betId": "bet-1",
        "marketId": MARKET_ID,
        "selectionId": SELECTION_ID,
        "side": "BACK",
        "status": status,
        "placedDate": "2026-09-21T09:54:55+00:00",
        "priceSize": {"price": 3.0, "size": 10.0},
        "averagePriceMatched": 3.1 if size_matched > 0 else 0.0,
        "sizeMatched": size_matched,
        "sizeRemaining": size_remaining,
        "customerOrderRef": provider_order_ref,
    }


def _cleared_row(provider_order_ref: str) -> dict[str, object]:
    return {
        "eventId": EVENT_ID,
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


def _capture(
    provider_order_ref: str,
    *,
    status: str,
    size_matched: float,
    size_remaining: float,
    include_cleared: bool,
):
    settled = [_cleared_row(provider_order_ref)] if include_cleared else []
    responses = [
        _response(
            [{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}],
            1,
        ),
        _response(
            {
                "currentOrders": [
                    _current_row(
                        provider_order_ref,
                        status=status,
                        size_matched=size_matched,
                        size_remaining=size_remaining,
                    )
                ],
                "moreAvailable": False,
            },
            2,
        ),
        _response({"clearedOrders": settled, "moreAvailable": False}, 3),
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


@pytest.mark.parametrize("include_cleared", [False, True])
def test_unknown_current_order_status_fails_closed(
    tmp_path: Path,
    include_cleared: bool,
) -> None:
    plan, ledger, provider_order_ref = _prepared(tmp_path)
    readback = _capture(
        provider_order_ref,
        status="UNKNOWN_PROVIDER_STATUS",
        size_matched=4.0,
        size_remaining=6.0,
        include_cleared=include_cleared,
    )

    with pytest.raises(RealizedMatchEvidenceError):
        resolve_betfair_realized_match(
            plan,
            ledger,
            readback,
            attempt_id=ATTEMPT_ID,
        )


@pytest.mark.parametrize("include_cleared", [False, True])
def test_execution_complete_with_remaining_size_fails_closed(
    tmp_path: Path,
    include_cleared: bool,
) -> None:
    plan, ledger, provider_order_ref = _prepared(tmp_path)
    readback = _capture(
        provider_order_ref,
        status="EXECUTION_COMPLETE",
        size_matched=4.0,
        size_remaining=6.0,
        include_cleared=include_cleared,
    )

    with pytest.raises(RealizedMatchEvidenceError):
        resolve_betfair_realized_match(
            plan,
            ledger,
            readback,
            attempt_id=ATTEMPT_ID,
        )


def test_executable_partial_current_order_remains_authoritative(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_order_ref = _prepared(tmp_path)
    evidence = resolve_betfair_realized_match(
        plan,
        ledger,
        _capture(
            provider_order_ref,
            status="EXECUTABLE",
            size_matched=4.0,
            size_remaining=6.0,
            include_cleared=False,
        ),
        attempt_id=ATTEMPT_ID,
    )

    evidence.assert_authoritative()
    assert evidence.source is RealizedMatchSource.CURRENT_ORDER
    assert evidence.provider_status == "EXECUTABLE"
    assert str(evidence.provider_matched_stake) == "4.0"
    assert evidence.finalized is False
