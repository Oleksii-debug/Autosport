from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autosport.betfair_supervised_execution import (
    BetfairPlaceOrdersAmbiguous,
    PlaceOrdersOutcome,
    _parse_place_orders_response,
    _report_outcome,
)
from autosport.real_execution_ledger import ExecutionAction


OBSERVED_AT = "2026-09-19T08:00:05+00:00"


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-response-truth",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.23456789",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("5.00"),
        quote_id="quote-1",
        quote_observed_at="2026-09-19T07:59:59+00:00",
        expires_at="2026-09-19T08:01:00+00:00",
    )


def _payload(
    action: ExecutionAction,
    *,
    execution_status: str,
    instruction_status: str,
    include_size_matched: bool,
    size_matched: str = "0",
    average_price_matched: str = "0",
    bet_id: str | None = None,
) -> bytes:
    report: dict[str, object] = {
        "status": instruction_status,
        "instruction": {
            "selectionId": int(action.selection_id),
            "handicap": 0,
            "side": action.side,
            "orderType": "LIMIT",
            "limitOrder": {
                "size": str(action.requested_stake),
                "price": str(action.requested_odds),
                "persistenceType": "LAPSE",
            },
        },
        "placedDate": OBSERVED_AT,
        "averagePriceMatched": average_price_matched,
    }
    if include_size_matched:
        report["sizeMatched"] = size_matched
    if bet_id is not None:
        report["betId"] = bet_id
    if instruction_status == "FAILURE":
        report["errorCode"] = "BET_TAKEN_OR_LAPSED"

    result: dict[str, object] = {
        "status": execution_status,
        "marketId": action.market_id,
        "instructionReports": [report],
    }
    if execution_status == "FAILURE":
        result["errorCode"] = "BET_ACTION_ERROR"

    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": result,
            "id": 1,
        }
    ).encode("utf-8")


def _parse(payload: bytes, action: ExecutionAction):
    return _parse_place_orders_response(
        payload,
        request_id=1,
        request_sha256="a" * 64,
        action=action,
        provider_order_ref="b" * 32,
        observed_at=OBSERVED_AT,
    )


def test_failure_without_size_matched_is_ambiguous_not_rejected() -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="FAILURE",
        instruction_status="FAILURE",
        include_size_matched=False,
    )

    with pytest.raises(BetfairPlaceOrdersAmbiguous, match="sizeMatched"):
        _parse(payload, action)


def test_success_without_size_matched_is_ambiguous_not_accepted() -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="SUCCESS",
        instruction_status="SUCCESS",
        include_size_matched=False,
        bet_id="bet-123",
    )

    with pytest.raises(BetfairPlaceOrdersAmbiguous, match="sizeMatched"):
        _parse(payload, action)


def test_explicit_zero_failure_remains_rejected() -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="FAILURE",
        instruction_status="FAILURE",
        include_size_matched=True,
        size_matched="0",
    )

    report = _parse(payload, action)

    assert report.instruction.size_matched == Decimal("0")
    assert _report_outcome(report, action) is PlaceOrdersOutcome.REJECTED
