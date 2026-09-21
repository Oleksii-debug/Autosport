from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport.betfair_supervised_execution import (
    BetfairPlaceOrdersAmbiguous,
    _parse_place_orders_response,
)


REQUEST_ID = 7
REQUEST_SHA256 = "a" * 64
PROVIDER_ORDER_REF = "b" * 32
OBSERVED_AT = "2026-09-21T19:00:01+00:00"


def _action() -> SimpleNamespace:
    return SimpleNamespace(
        bookmaker_id="betfair",
        account_id="account-a",
        action_id="action-a",
        market_id="1.234",
        selection_id="123",
        side="BACK",
        requested_odds=Decimal("2.5"),
        requested_stake=Decimal("10"),
    )


def _instruction_echo() -> dict[str, object]:
    return {
        "selectionId": 123,
        "side": "BACK",
        "orderType": "LIMIT",
        "limitOrder": {
            "price": 2.5,
            "size": 10,
            "persistenceType": "LAPSE",
        },
    }


def _payload(
    *,
    report_status: str,
    instruction_status: str,
    size_matched: object,
    average_price_matched: object,
    instruction_error: str | None = None,
    bet_id: str | None = None,
) -> bytes:
    instruction_report: dict[str, object] = {
        "status": instruction_status,
        "instruction": _instruction_echo(),
        "sizeMatched": size_matched,
        "averagePriceMatched": average_price_matched,
    }
    if instruction_error is not None:
        instruction_report["errorCode"] = instruction_error
    if bet_id is not None:
        instruction_report["betId"] = bet_id
        instruction_report["placedDate"] = "2026-09-21T19:00:00Z"

    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": REQUEST_ID,
            "result": {
                "marketId": "1.234",
                "status": report_status,
                "instructionReports": [instruction_report],
            },
        }
    ).encode("utf-8")


def _parse(payload: bytes) -> object:
    return _parse_place_orders_response(
        payload,
        request_id=REQUEST_ID,
        request_sha256=REQUEST_SHA256,
        action=_action(),
        provider_order_ref=PROVIDER_ORDER_REF,
        observed_at=OBSERVED_AT,
    )


def test_top_level_success_cannot_terminalize_failed_instruction() -> None:
    payload = _payload(
        report_status="SUCCESS",
        instruction_status="FAILURE",
        size_matched=0,
        average_price_matched=0,
        instruction_error="ERROR_IN_ORDER",
    )

    with pytest.raises(BetfairPlaceOrdersAmbiguous):
        _parse(payload)


def test_top_level_failure_cannot_accept_successful_instruction() -> None:
    payload = _payload(
        report_status="FAILURE",
        instruction_status="SUCCESS",
        size_matched=10,
        average_price_matched=2.5,
        bet_id="123456789",
    )

    with pytest.raises(BetfairPlaceOrdersAmbiguous):
        _parse(payload)
