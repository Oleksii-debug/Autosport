from __future__ import annotations

import json
from decimal import Decimal

from autosport.betfair_supervised_execution import (
    PlaceOrdersOutcome,
    _parse_place_orders_response,
    _report_outcome,
)
from autosport.real_execution_ledger import ExecutionAction


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-mixed-status",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.23456789",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("5.00"),
        quote_id="quote-1",
        quote_observed_at="2026-09-21T18:59:50+00:00",
        expires_at="2026-09-21T19:01:00+00:00",
    )


def test_single_instruction_processed_with_errors_uses_failure_instruction_truth() -> None:
    action = _action()
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {
                "status": "PROCESSED_WITH_ERRORS",
                "marketId": action.market_id,
                "instructionReports": [
                    {
                        "status": "FAILURE",
                        "errorCode": "BET_ACTION_ERROR",
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
                        "sizeMatched": 0,
                        "averagePriceMatched": 0,
                    }
                ],
            },
            "id": 1,
        }
    ).encode("utf-8")

    report = _parse_place_orders_response(
        payload,
        request_id=1,
        request_sha256="a" * 64,
        action=action,
        provider_order_ref="b" * 32,
        observed_at="2026-09-21T19:00:00+00:00",
    )

    assert report.status == "PROCESSED_WITH_ERRORS"
    assert report.instruction.status == "FAILURE"
    # PROCESSED_WITH_ERRORS is a legal request-level status even when all
    # instructions fail. This fixture intentionally lacks the exact terminal
    # rejection proof (provider betId + execution-complete disposition and
    # matching top-level/instruction error contract), so it must remain
    # UNKNOWN/readback-required rather than becoming a synthetic rejection.
    assert _report_outcome(report, action) is PlaceOrdersOutcome.UNKNOWN
