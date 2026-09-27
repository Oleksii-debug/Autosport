from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autosport.betfair_supervised_execution import (
    BetfairPlaceOrdersAmbiguous,
    BetfairSupervisedExecutionError,
    _parse_place_orders_response,
    _validate_betfair_place_action,
)
from autosport.real_execution_ledger import ExecutionAction


OBSERVED_AT = "2026-09-27T12:00:00+00:00"
EXPIRES_AT = "2026-09-27T12:01:00+00:00"


def _action(*, selection_id: str = "42") -> ExecutionAction:
    return ExecutionAction(
        action_id="action-limit-boundary",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.23456789",
        selection_id=selection_id,
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("10.00"),
        quote_id="quote-1",
        quote_observed_at=OBSERVED_AT,
        expires_at=EXPIRES_AT,
    )


def _response(action: ExecutionAction, *, average: str) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "status": "SUCCESS",
                "marketId": action.market_id,
                "instructionReports": [
                    {
                        "status": "SUCCESS",
                        "betId": "bet-1",
                        "placedDate": OBSERVED_AT,
                        "averagePriceMatched": average,
                        "sizeMatched": "10.00",
                        "instruction": {
                            "selectionId": int(action.selection_id),
                            "handicap": 0,
                            "side": action.side,
                            "orderType": "LIMIT",
                            "limitOrder": {
                                "size": "10.00",
                                "price": "2.00",
                                "persistenceType": "LAPSE",
                            },
                            "customerOrderRef": "0123",
                        },
                    }
                ],
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")


def test_selection_id_accepts_provider_signed_long_maximum() -> None:
    maximum = "9223372036854775807"
    assert _validate_betfair_place_action(_action(selection_id=maximum)) == int(maximum)


@pytest.mark.parametrize(
    "selection_id,match",
    [
        ("9223372036854775808", "exceeds signed-long provider domain"),
        ("٤٢", "canonical positive integer text"),
        ("042", "canonical positive integer text"),
    ],
)
def test_selection_id_rejects_values_outside_canonical_provider_domain(
    selection_id: str,
    match: str,
) -> None:
    with pytest.raises(BetfairSupervisedExecutionError, match=match):
        _validate_betfair_place_action(_action(selection_id=selection_id))


def test_matched_back_limit_below_requested_odds_is_ambiguous() -> None:
    action = _action()
    with pytest.raises(
        BetfairPlaceOrdersAmbiguous,
        match="average price is below requested BACK LIMIT price",
    ):
        _parse_place_orders_response(
            _response(action, average="1.99"),
            request_id=1,
            request_sha256="a" * 64,
            action=action,
            provider_order_ref="0123",
            observed_at=OBSERVED_AT,
        )


def test_matched_back_limit_at_requested_odds_remains_exactly_parseable() -> None:
    action = _action()
    report = _parse_place_orders_response(
        _response(action, average="2.00"),
        request_id=1,
        request_sha256="a" * 64,
        action=action,
        provider_order_ref="0123",
        observed_at=OBSERVED_AT,
    )
    assert report.instruction.average_price_matched == Decimal("2.00")
    assert report.instruction.size_matched == Decimal("10.00")
