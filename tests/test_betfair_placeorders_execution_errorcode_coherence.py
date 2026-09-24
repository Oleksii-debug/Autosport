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



def _failure_payload(
    *,
    execution_error_code: str,
    instruction_error_code: str,
    order_status: str | None = "EXECUTION_COMPLETE",
    average_price_matched: int = 0,
) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {
                "status": "FAILURE",
                "errorCode": execution_error_code,
                "marketId": "1.23456789",
                "instructionReports": [
                    {
                        "status": "FAILURE",
                        "errorCode": instruction_error_code,
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
                        "betId": "bet-rejection-123",
                        **(
                            {"orderStatus": order_status}
                            if order_status is not None
                            else {}
                        ),
                        "placedDate": OBSERVED_AT,
                        "averagePriceMatched": average_price_matched,
                        "sizeMatched": 0,
                    }
                ],
            },
            "id": 1,
        }
    ).encode("utf-8")


def _parse_failure(
    *,
    execution_error_code: str,
    instruction_error_code: str,
):
    action = _action()
    report = _parse_place_orders_response(
        _failure_payload(
            execution_error_code=execution_error_code,
            instruction_error_code=instruction_error_code,
        ),
        request_id=1,
        request_sha256="a" * 64,
        action=action,
        provider_order_ref="b" * 32,
        observed_at=OBSERVED_AT,
    )
    return action, report


@pytest.mark.parametrize(
    ("execution_error_code", "instruction_error_code"),
    (
        ("SERVICE_UNAVAILABLE", "BET_TAKEN_OR_LAPSED"),
        ("ERROR_IN_MATCHER", "BET_TAKEN_OR_LAPSED"),
        ("REGULATOR_IS_NOT_AVAILABLE", "BET_TAKEN_OR_LAPSED"),
        ("BET_ACTION_ERROR", "BET_IN_PROGRESS"),
        ("FUTURE_EXECUTION_CODE", "BET_TAKEN_OR_LAPSED"),
        ("BET_ACTION_ERROR", "FUTURE_INSTRUCTION_CODE"),
    ),
)
def test_unqualified_failure_error_codes_require_readback(
    execution_error_code: str,
    instruction_error_code: str,
) -> None:
    action, report = _parse_failure(
        execution_error_code=execution_error_code,
        instruction_error_code=instruction_error_code,
    )

    assert report.instruction.bet_id == "bet-rejection-123"
    assert report.instruction.size_matched == Decimal("0")
    assert _report_outcome(report, action) is PlaceOrdersOutcome.UNKNOWN


def test_documented_bet_action_rejection_remains_terminal_rejected() -> None:
    action, report = _parse_failure(
        execution_error_code="BET_ACTION_ERROR",
        instruction_error_code="BET_TAKEN_OR_LAPSED",
    )

    assert report.instruction.order_status == "EXECUTION_COMPLETE"
    assert _report_outcome(report, action) is PlaceOrdersOutcome.REJECTED



def test_failure_with_positive_average_is_ambiguous() -> None:
    action = _action()

    with pytest.raises(
        BetfairPlaceOrdersAmbiguous,
        match="internally inconsistent",
    ):
        _parse_place_orders_response(
            _failure_payload(
                execution_error_code="BET_ACTION_ERROR",
                instruction_error_code="BET_TAKEN_OR_LAPSED",
                average_price_matched=2,
            ),
            request_id=1,
            request_sha256="a" * 64,
            action=action,
            provider_order_ref="b" * 32,
            observed_at=OBSERVED_AT,
        )


def test_qualified_failure_without_terminal_order_status_requires_readback() -> None:
    action = _action()
    report = _parse_place_orders_response(
        _failure_payload(
            execution_error_code="BET_ACTION_ERROR",
            instruction_error_code="BET_TAKEN_OR_LAPSED",
            order_status=None,
        ),
        request_id=1,
        request_sha256="a" * 64,
        action=action,
        provider_order_ref="b" * 32,
        observed_at=OBSERVED_AT,
    )

    assert report.instruction.order_status is None
    assert _report_outcome(report, action) is PlaceOrdersOutcome.UNKNOWN



def test_success_zero_match_with_positive_average_is_ambiguous() -> None:
    action = _action()

    with pytest.raises(
        BetfairPlaceOrdersAmbiguous,
        match="zero matched stake cannot claim positive average price",
    ):
        _parse_place_orders_response(
            _success_payload(
                size_matched=0,
                average_price_matched=2,
            ),
            request_id=1,
            request_sha256="a" * 64,
            action=action,
            provider_order_ref="b" * 32,
            observed_at=OBSERVED_AT,
        )
