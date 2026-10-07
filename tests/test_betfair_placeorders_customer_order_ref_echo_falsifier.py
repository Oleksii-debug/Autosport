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
EXPECTED_PROVIDER_ORDER_REF = "a" * 32
CONFLICTING_PROVIDER_ORDER_REF = "b" * 32


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-customer-order-ref-echo",
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
    customer_order_ref: str | None,
) -> bytes:
    instruction: dict[str, object] = {
        "selectionId": int(action.selection_id),
        "handicap": 0,
        "side": action.side,
        "orderType": "LIMIT",
        "limitOrder": {
            "size": 5,
            "price": 2,
            "persistenceType": "LAPSE",
        },
    }
    if customer_order_ref is not None:
        instruction["customerOrderRef"] = customer_order_ref

    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {
                "status": "SUCCESS",
                "marketId": action.market_id,
                "instructionReports": [
                    {
                        "status": "SUCCESS",
                        "instruction": instruction,
                        "betId": "bet-customer-ref-123",
                        "orderStatus": "EXECUTION_COMPLETE",
                        "placedDate": OBSERVED_AT,
                        "averagePriceMatched": 2,
                        "sizeMatched": 5,
                    }
                ],
            },
            "id": 1,
        }
    ).encode("utf-8")


def _parse(payload: bytes, action: ExecutionAction):
    return _parse_place_orders_response(
        payload,
        request_id=1,
        request_sha256="c" * 64,
        action=action,
        provider_order_ref=EXPECTED_PROVIDER_ORDER_REF,
        observed_at=OBSERVED_AT,
    )


def test_conflicting_present_customer_order_ref_echo_fails_closed() -> None:
    action = _action()

    with pytest.raises(BetfairPlaceOrdersAmbiguous):
        _parse(
            _payload(
                action,
                customer_order_ref=CONFLICTING_PROVIDER_ORDER_REF,
            ),
            action,
        )


def test_matching_customer_order_ref_echo_preserves_valid_success() -> None:
    action = _action()

    report = _parse(
        _payload(
            action,
            customer_order_ref=EXPECTED_PROVIDER_ORDER_REF,
        ),
        action,
    )

    assert _report_outcome(report, action) is PlaceOrdersOutcome.ACCEPTED


def test_omitted_customer_order_ref_echo_is_not_made_mandatory() -> None:
    action = _action()

    report = _parse(
        _payload(action, customer_order_ref=None),
        action,
    )

    assert _report_outcome(report, action) is PlaceOrdersOutcome.ACCEPTED
