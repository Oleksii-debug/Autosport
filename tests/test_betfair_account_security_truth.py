from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
    _decode_json,
)
from autosport.bookmaker_capability import BookmakerCapability, BookmakerPositionState


NOW = datetime(2026, 9, 17, 18, 45, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        assert url.startswith("https://api.betfair.com/exchange/")
        assert headers["X-Authentication"] == "session-secret"
        assert headers["X-Application"] == "app-secret"
        assert b"session-secret" not in body
        assert b"app-secret" not in body
        return self.responses.pop(0)


def rpc(result: object, request_id: object) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def client_for(*responses: bytes) -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FakeTransport(list(responses)),
        clock=lambda: NOW,
        venue_id="betfair",
        account_id="acct-1",
    )


def account_details(request_id: int = 1) -> bytes:
    return rpc(
        {
            "currencyCode": "EUR",
            "localeCode": "en",
            "region": "SK",
            "timezone": "Europe/Bratislava",
        },
        request_id,
    )


def current_order(bet_id: str, *, size_matched: float = 1.0) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": "1.100",
        "selectionId": 1,
        "side": "BACK",
        "status": "EXECUTABLE",
        "placedDate": "2026-09-17T18:00:00+00:00",
        "priceSize": {"price": 2.0, "size": size_matched},
        "averagePriceMatched": 2.0 if size_matched else 0,
        "sizeMatched": size_matched,
        "sizeRemaining": 0,
    }


def cleared_order(bet_id: str) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": "1.100",
        "selectionId": 1,
        "side": "BACK",
        "placedDate": "2026-09-16T18:00:00+00:00",
        "settledDate": "2026-09-17T18:00:00+00:00",
        "priceRequested": 2.0,
        "priceMatched": 2.0,
        "sizeSettled": 1.0,
        "profit": 1.0,
    }


def test_provider_controlled_error_message_redacts_both_known_credentials() -> None:
    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "error": {
                "code": -32099,
                "message": "echo app-secret and session-secret from upstream",
            },
            "id": 1,
        }
    ).encode("utf-8")
    client = client_for(raw)

    with pytest.raises(BetfairReadOnlyError) as captured:
        client.read_account_funds()

    message = str(captured.value)
    assert "app-secret" not in message
    assert "session-secret" not in message
    assert message.count("<redacted>") == 2


@pytest.mark.parametrize("response_id", [True, 1.0, "1", None])
def test_response_id_requires_an_actual_integer(response_id: object) -> None:
    client = client_for(rpc({}, response_id))

    with pytest.raises(BetfairReadOnlyError, match="response id does not match request id"):
        client.read_account_funds()


def test_duplicate_key_diagnostic_does_not_echo_provider_controlled_key() -> None:
    payload = b'{"jsonrpc":"2.0","result":{"app-secret":1,"app-secret":2},"id":1}'

    with pytest.raises(BetfairReadOnlyError) as captured:
        _decode_json(payload)

    message = str(captured.value)
    assert "duplicate object key" in message
    assert "app-secret" not in message


@pytest.mark.parametrize(
    "payload",
    [
        b'{"jsonrpc":"2.0","result":{},"id":1,"id":1}',
        b'{"jsonrpc":"2.0","result":{"availableToBetBalance":1,"availableToBetBalance":2},"id":1}',
        b'{"jsonrpc":"2.0","result":{"currentOrders":[],"moreAvailable":false,"moreAvailable":true},"id":1}',
    ],
)
def test_duplicate_keys_at_authority_boundaries_fail_closed(payload: bytes) -> None:
    with pytest.raises(BetfairReadOnlyError, match="duplicate object key"):
        _decode_json(payload)


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_all_nonstandard_numeric_constants_fail_closed(constant: bytes) -> None:
    payload = b'{"jsonrpc":"2.0","result":{"value":' + constant + b'},"id":1}'
    with pytest.raises(BetfairReadOnlyError, match="non-standard numeric constant"):
        _decode_json(payload)


def test_duplicate_bet_id_diagnostic_does_not_echo_provider_controlled_id() -> None:
    secret_order = current_order("session-secret")
    client = client_for(
        rpc(
            {"currentOrders": [secret_order, dict(secret_order)], "moreAvailable": False},
            1,
        )
    )

    with pytest.raises(BetfairReadOnlyError) as captured:
        client.read_current_orders_page()

    message = str(captured.value)
    assert "duplicate bet_id" in message
    assert "session-secret" not in message


def test_cross_state_overlap_diagnostic_does_not_echo_provider_controlled_id() -> None:
    secret_id = "app-secret"
    client = client_for(
        account_details(),
        rpc({"currentOrders": [current_order(secret_id)], "moreAvailable": False}, 2),
        rpc({"clearedOrders": [cleared_order(secret_id)], "moreAvailable": False}, 3),
    )

    with pytest.raises(BetfairReadOnlyError) as captured:
        client.read_account_snapshot(
            frozenset(
                {
                    BookmakerCapability.OPEN_POSITIONS_READ,
                    BookmakerCapability.SETTLED_POSITIONS_READ,
                }
            )
        )

    message = str(captured.value)
    assert "both OPEN and SETTLED" in message
    assert secret_id not in message


def test_snapshot_excludes_unmatched_order_from_open_positions_and_preserves_lay_side() -> None:
    current_orders = rpc(
        {
            "currentOrders": [
                {
                    "betId": "unmatched",
                    "marketId": "1.100",
                    "selectionId": 1,
                    "side": "LAY",
                    "status": "EXECUTABLE",
                    "placedDate": "2026-09-17T18:00:00+00:00",
                    "priceSize": {"price": 3.0, "size": 10.0},
                    "averagePriceMatched": 0,
                    "sizeMatched": 0,
                    "sizeRemaining": 10.0,
                },
                {
                    "betId": "matched",
                    "marketId": "1.101",
                    "selectionId": 2,
                    "side": "LAY",
                    "status": "EXECUTABLE",
                    "placedDate": "2026-09-17T18:01:00+00:00",
                    "priceSize": {"price": 3.0, "size": 5.0},
                    "averagePriceMatched": 2.8,
                    "sizeMatched": 2.0,
                    "sizeRemaining": 3.0,
                },
            ],
            "moreAvailable": False,
        },
        2,
    )
    client = client_for(account_details(), current_orders)

    snapshot = client.read_account_snapshot(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ})
    )

    assert len(snapshot.open_positions) == 1
    position = snapshot.open_positions[0]
    assert position.external_position_id == "matched"
    assert position.state is BookmakerPositionState.OPEN
    assert position.provider_side == "LAY"
    assert position.provider_amount == Decimal("2.0")
    assert position.provider_amount_semantics == "betfair_size_matched"
    assert position.decimal_odds == Decimal("2.8")
    assert position.gross_return is None


def test_settled_provider_profit_is_not_promoted_to_canonical_return_or_pnl() -> None:
    cleared_orders = rpc(
        {
            "clearedOrders": [
                {
                    "betId": "settled-lay",
                    "marketId": "1.200",
                    "selectionId": 3,
                    "side": "LAY",
                    "placedDate": "2026-09-16T18:00:00+00:00",
                    "settledDate": "2026-09-17T18:00:00+00:00",
                    "priceRequested": 2.6,
                    "priceMatched": 2.5,
                    "sizeSettled": 5.0,
                    "profit": 99.0,
                }
            ],
            "moreAvailable": False,
        },
        2,
    )
    client = client_for(account_details(), cleared_orders)

    snapshot = client.read_account_snapshot(
        frozenset({BookmakerCapability.SETTLED_POSITIONS_READ})
    )

    position = snapshot.settled_positions[0]
    assert position.state is BookmakerPositionState.SETTLED
    assert position.provider_side == "LAY"
    assert position.provider_amount == Decimal("5.0")
    assert position.provider_amount_semantics == "betfair_size_settled"
    assert position.decimal_odds == Decimal("2.5")
    assert position.gross_return is None


def test_write_operations_remain_unreachable() -> None:
    client = client_for()
    for name in ("place_orders", "cancel_orders", "replace_orders", "update_orders"):
        assert not hasattr(client, name)
