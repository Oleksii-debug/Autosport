from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autosport.betfair_supervised_execution import (
    BetfairPlaceOrdersAmbiguous,
    _parse_place_orders_response,
)
from autosport.real_execution_ledger import ExecutionAction


OBSERVED_AT = "2026-09-19T08:00:05+00:00"


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-execution-errorcode-coherence",
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


def _success_payload(*, size_matched: int, average_price_matched: int) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {
                "status": "SUCCESS",
                "errorCode": "BET_ACTION_ERROR",
                "marketId": "1.23456789",
                "instructionReports": [
                    {
                        "status": "SUCCESS",
                        "instruction": {
                            "selectionId": 42,
                            "handicap": 0,
                            "side": "BACK",
                            "orderType": "LIMIT",
                            "limitOrder": {
                                "size": 5,
                                "price": 2,
                                "persistenceType": "LAPSE",
                            },
                        },
                        "betId": "bet-123",
                        "orderStatus": (
                            "EXECUTION_COMPLETE"
                            if size_matched
                            else "EXECUTABLE"
                        ),
                        "placedDate": OBSERVED_AT,
                        "averagePriceMatched": average_price_matched,
                        "sizeMatched": size_matched,
                    }
                ],
            },
            "id": 1,
        }
    ).encode("utf-8")


@pytest.mark.parametrize(
    ("size_matched", "average_price_matched"),
    (
        (5, 2),
        (0, 0),
    ),
)
def test_success_with_execution_error_code_fails_closed(
    size_matched: int,
    average_price_matched: int,
) -> None:
    action = _action()

    with pytest.raises(
        BetfairPlaceOrdersAmbiguous,
        match="must not include errorCode",
    ):
        _parse_place_orders_response(
            _success_payload(
                size_matched=size_matched,
                average_price_matched=average_price_matched,
            ),
            request_id=1,
            request_sha256="a" * 64,
            action=action,
            provider_order_ref="b" * 32,
            observed_at=OBSERVED_AT,
        )
