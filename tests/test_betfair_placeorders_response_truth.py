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
    size_matched: object = 0,
    average_price_matched: object = 0,
    echoed_size: object | None = None,
    echoed_price: object | None = None,
    bet_id: str | None = None,
    order_status: str | None = None,
    handicap: object = 0,
    persistence_type: str = "LAPSE",
    response_id: object = 1,
    selection_id: object | None = None,
) -> bytes:
    report: dict[str, object] = {
        "status": instruction_status,
        "instruction": {
            "selectionId": (
                int(action.selection_id)
                if selection_id is None
                else selection_id
            ),
            "handicap": handicap,
            "side": action.side,
            "orderType": "LIMIT",
            "limitOrder": {
                "size": (
                    float(action.requested_stake)
                    if echoed_size is None
                    else echoed_size
                ),
                "price": (
                    float(action.requested_odds)
                    if echoed_price is None
                    else echoed_price
                ),
                "persistenceType": persistence_type,
            },
        },
        "placedDate": OBSERVED_AT,
        "averagePriceMatched": average_price_matched,
    }
    if include_size_matched:
        report["sizeMatched"] = size_matched
    if bet_id is not None:
        report["betId"] = bet_id
    if order_status is not None:
        report["orderStatus"] = order_status
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
            "id": response_id,
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
        size_matched=0,
        bet_id="bet-rejected-123",
        order_status="EXECUTION_COMPLETE",
    )

    report = _parse(payload, action)

    assert report.instruction.size_matched == Decimal("0")
    assert report.instruction.order_status == "EXECUTION_COMPLETE"
    assert _report_outcome(report, action) is PlaceOrdersOutcome.REJECTED


def test_terminal_failure_without_bet_id_requires_readback() -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="FAILURE",
        instruction_status="FAILURE",
        include_size_matched=True,
        size_matched=0,
        bet_id=None,
        order_status="EXECUTION_COMPLETE",
    )

    report = _parse(payload, action)

    assert report.instruction.bet_id is None
    assert _report_outcome(report, action) is PlaceOrdersOutcome.UNKNOWN


@pytest.mark.parametrize(
    ("order_status", "expected"),
    (
        (None, PlaceOrdersOutcome.UNKNOWN),
        ("EXECUTABLE", PlaceOrdersOutcome.UNKNOWN),
        ("EXECUTION_COMPLETE", PlaceOrdersOutcome.PARTIAL),
    ),
)
def test_partial_fill_requires_explicit_terminal_order_status(
    order_status: str | None,
    expected: PlaceOrdersOutcome,
) -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="SUCCESS",
        instruction_status="SUCCESS",
        include_size_matched=True,
        size_matched=2,
        average_price_matched=2,
        bet_id="bet-partial-123",
        order_status=order_status,
    )

    report = _parse(payload, action)

    assert report.instruction.size_matched == Decimal("2")
    assert _report_outcome(report, action) is expected


def test_failure_with_executable_order_status_is_ambiguous() -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="FAILURE",
        instruction_status="FAILURE",
        include_size_matched=True,
        size_matched=0,
        bet_id="bet-contradictory",
        order_status="EXECUTABLE",
    )

    with pytest.raises(BetfairPlaceOrdersAmbiguous, match="EXECUTABLE"):
        _parse(payload, action)


def test_success_zero_fill_with_bet_id_is_known_placed_unmatched() -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="SUCCESS",
        instruction_status="SUCCESS",
        include_size_matched=True,
        size_matched=0,
        average_price_matched=0,
        bet_id="bet-unmatched",
        order_status="EXECUTABLE",
    )

    report = _parse(payload, action)

    assert report.instruction.order_status == "EXECUTABLE"
    assert _report_outcome(report, action) is PlaceOrdersOutcome.PLACED_UNMATCHED

@pytest.mark.parametrize(
    ("handicap", "persistence_type"),
    (
        (1, "LAPSE"),
        (0, "PERSIST"),
    ),
)
def test_response_echo_rejects_handicap_or_persistence_drift(
    handicap: object,
    persistence_type: str,
) -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="SUCCESS",
        instruction_status="SUCCESS",
        include_size_matched=True,
        size_matched=5,
        average_price_matched=2,
        bet_id="bet-123",
        handicap=handicap,
        persistence_type=persistence_type,
    )

    with pytest.raises(
        BetfairPlaceOrdersAmbiguous,
        match="does not bind exact action",
    ):
        _parse(payload, action)

@pytest.mark.parametrize("response_id", (True, 1.5, "1"))
def test_response_id_rejects_coercible_non_integer_wire_identity(
    response_id: object,
) -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="SUCCESS",
        instruction_status="SUCCESS",
        include_size_matched=True,
        size_matched=5,
        average_price_matched=2,
        bet_id="bet-123",
        response_id=response_id,
    )

    with pytest.raises(
        BetfairPlaceOrdersAmbiguous,
        match="exact JSON-RPC request",
    ):
        _parse(payload, action)


@pytest.mark.parametrize("selection_id", (42.5, "42", True))
def test_response_echo_rejects_coercible_selection_identity(
    selection_id: object,
) -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="SUCCESS",
        instruction_status="SUCCESS",
        include_size_matched=True,
        size_matched=5,
        average_price_matched=2,
        bet_id="bet-123",
        selection_id=selection_id,
    )

    with pytest.raises(
        BetfairPlaceOrdersAmbiguous,
        match="echoed selection is malformed",
    ):
        _parse(payload, action)


@pytest.mark.parametrize("handicap", (False, "0"))
def test_response_echo_rejects_coercible_handicap_identity(
    handicap: object,
) -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="SUCCESS",
        instruction_status="SUCCESS",
        include_size_matched=True,
        size_matched=5,
        average_price_matched=2,
        bet_id="bet-123",
        handicap=handicap,
    )

    with pytest.raises(
        BetfairPlaceOrdersAmbiguous,
        match="does not bind exact action",
    ):
        _parse(payload, action)

@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("echoed_price", "2.00"),
        ("echoed_price", True),
        ("echoed_size", "5.00"),
        ("echoed_size", True),
        ("size_matched", "5.00"),
        ("size_matched", True),
        ("average_price_matched", "2.00"),
        ("average_price_matched", True),
    ),
)
def test_response_authority_rejects_coercible_non_numeric_wire_values(
    field: str,
    value: object,
) -> None:
    action = _action()
    kwargs: dict[str, object] = {
        "size_matched": 5,
        "average_price_matched": 2,
    }
    kwargs[field] = value
    payload = _payload(
        action,
        execution_status="SUCCESS",
        instruction_status="SUCCESS",
        include_size_matched=True,
        bet_id="bet-123",
        **kwargs,
    )

    with pytest.raises(BetfairPlaceOrdersAmbiguous):
        _parse(payload, action)


def test_response_echo_accepts_equivalent_integral_numeric_wire_values() -> None:
    action = _action()
    payload = _payload(
        action,
        execution_status="SUCCESS",
        instruction_status="SUCCESS",
        include_size_matched=True,
        size_matched=5,
        average_price_matched=2,
        bet_id="bet-123",
        response_id=1.0,
        selection_id=42.0,
        handicap=0.0,
    )

    report = _parse(payload, action)

    assert report.request_id == 1
    assert report.instruction.bet_id == "bet-123"
    assert _report_outcome(report, action) is PlaceOrdersOutcome.ACCEPTED

