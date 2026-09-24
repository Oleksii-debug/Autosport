from __future__ import annotations

import json
from decimal import Decimal

from autosport.betfair_supervised_execution import _parse_place_orders_response
from autosport.real_execution_ledger import ExecutionAction


OBSERVED_AT = "2026-09-21T19:30:00+00:00"


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-terminal-identity",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.23456789",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("5.00"),
        quote_id="quote-1",
        quote_observed_at="2026-09-21T19:29:50+00:00",
        expires_at="2026-09-21T19:31:00+00:00",
    )


def _payload(action: ExecutionAction, *, bet_id: str | None) -> bytes:
    report: dict[str, object] = {
        "status": "FAILURE",
        "errorCode": "BET_TAKEN_OR_LAPSED",
        "instruction": {
            "selectionId": int(action.selection_id),
            "handicap": 0,
            "side": action.side,
            "orderType": "LIMIT",
            "limitOrder": {
                "size": float(action.requested_stake),
                "price": float(action.requested_odds),
                "persistenceType": "LAPSE",
            },
        },
        "placedDate": OBSERVED_AT,
        "orderStatus": "EXECUTION_COMPLETE",
        "sizeMatched": 0,
        "averagePriceMatched": 0,
    }
    if bet_id is not None:
        report["betId"] = bet_id
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {
                "status": "FAILURE",
                "errorCode": "BET_ACTION_ERROR",
                "marketId": action.market_id,
                "instructionReports": [report],
            },
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


def test_terminal_order_state_without_bet_id_remains_parseable() -> None:
    action = _action()

    report = _parse(_payload(action, bet_id=None), action)

    assert report.instruction.bet_id is None
    assert report.instruction.order_status == "EXECUTION_COMPLETE"
    assert report.instruction.size_matched == Decimal("0")


def test_documented_terminal_failure_shape_with_bet_id_remains_parseable() -> None:
    action = _action()

    report = _parse(_payload(action, bet_id="253638292039"), action)

    assert report.instruction.bet_id == "253638292039"
    assert report.instruction.size_matched == Decimal("0")
