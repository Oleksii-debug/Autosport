from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json

import pytest

from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BETTING_JSON_RPC_ENDPOINT,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)


FIXED_NOW = datetime(2026, 9, 17, 17, 30, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def client_for(*responses: bytes):
    transport = FakeTransport(list(responses))
    credentials = BetfairSessionCredentials("app-secret", "session-secret")
    client = BetfairReadOnlyClient(
        credentials,
        transport=transport,
        clock=lambda: FIXED_NOW,
    )
    return client, transport


def test_credentials_and_client_repr_never_expose_secrets():
    credentials = BetfairSessionCredentials("app-secret", "session-secret")
    client = BetfairReadOnlyClient(
        credentials,
        transport=FakeTransport([]),
        clock=lambda: FIXED_NOW,
    )

    assert "app-secret" not in repr(credentials)
    assert "session-secret" not in repr(credentials)
    assert "app-secret" not in repr(client)
    assert "session-secret" not in repr(client)


def test_account_funds_preserves_provider_fields_as_exact_decimal_and_payload_hash():
    raw = (
        b'{"jsonrpc":"2.0","result":{"availableToBetBalance":100.10,'
        b'"exposure":-12.34,"retainedCommission":0.05,'
        b'"exposureLimit":-5000.00},"id":1}'
    )
    client, transport = client_for(raw)

    funds = client.read_account_funds()

    assert funds.available_to_bet_balance == Decimal("100.10")
    assert funds.exposure == Decimal("-12.34")
    assert funds.retained_commission == Decimal("0.05")
    assert funds.exposure_limit == Decimal("-5000.00")
    assert funds.evidence.source_payload_sha256 == sha256(raw).hexdigest()
    assert funds.evidence.observed_at == FIXED_NOW.isoformat()

    call = transport.calls[0]
    assert call["url"] == ACCOUNT_JSON_RPC_ENDPOINT
    assert call["headers"]["X-Application"] == "app-secret"
    assert call["headers"]["X-Authentication"] == "session-secret"
    assert b"app-secret" not in call["body"]
    assert b"session-secret" not in call["body"]
    request = json.loads(call["body"])
    assert request["method"] == "AccountAPING/v1.0/getAccountFunds"
    assert request["params"] == {}
    assert request["id"] == 1


def test_account_details_keeps_only_non_pii_metadata():
    client, _ = client_for(
        response(
            {
                "currencyCode": "GBP",
                "firstName": "DoNotPersist",
                "lastName": "DoNotPersist",
                "localeCode": "en",
                "region": "GBR",
                "timezone": "Europe/London",
            },
            1,
        )
    )

    details = client.read_account_details()

    assert details.currency_code == "GBP"
    assert details.locale_code == "en"
    assert details.region == "GBR"
    assert details.timezone_name == "Europe/London"
    assert not hasattr(details, "first_name")
    assert not hasattr(details, "last_name")


def test_current_orders_paginates_without_float_money_or_duplicate_identity():
    first = response(
        {
            "currentOrders": [
                {
                    "betId": "bet-1",
                    "marketId": "1.123",
                    "selectionId": 10,
                    "side": "BACK",
                    "status": "EXECUTABLE",
                    "placedDate": "2026-09-17T17:00:00+00:00",
                    "priceSize": {"price": 2.10, "size": 3.25},
                    "averagePriceMatched": 2.00,
                    "sizeMatched": 1.25,
                    "sizeRemaining": 2.00,
                    "customerOrderRef": "ord-1",
                }
            ],
            "moreAvailable": True,
        },
        1,
    )
    second = response(
        {
            "currentOrders": [
                {
                    "betId": "bet-2",
                    "marketId": "1.124",
                    "selectionId": 11,
                    "side": "LAY",
                    "status": "EXECUTABLE",
                    "placedDate": "2026-09-17T17:01:00+00:00",
                    "priceSize": {"price": 3.40, "size": 4.50},
                    "averagePriceMatched": 0,
                    "sizeMatched": 0,
                    "sizeRemaining": 4.50,
                    "customerStrategyRef": "strategy-a",
                }
            ],
            "moreAvailable": False,
        },
        2,
    )
    client, transport = client_for(first, second)

    orders = client.read_all_current_orders(page_size=1)

    assert [order.bet_id for order in orders] == ["bet-1", "bet-2"]
    assert orders[0].price == Decimal("2.1")
    assert orders[0].requested_size == Decimal("3.25")
    assert orders[0].size_matched == Decimal("1.25")
    assert orders[1].price == Decimal("3.4")
    assert [
        json.loads(call["body"])["params"]["fromRecord"]
        for call in transport.calls
    ] == [0, 1]
    assert all(call["url"] == BETTING_JSON_RPC_ENDPOINT for call in transport.calls)


def test_current_orders_duplicate_bet_id_across_pages_fails_closed():
    page = {
        "currentOrders": [
            {
                "betId": "same",
                "marketId": "1.1",
                "selectionId": 1,
                "side": "BACK",
                "status": "EXECUTABLE",
                "placedDate": "2026-09-17T17:00:00+00:00",
                "priceSize": {"price": 2.0, "size": 1.0},
                "averagePriceMatched": 0,
                "sizeMatched": 0,
                "sizeRemaining": 1.0,
            }
        ],
        "moreAvailable": True,
    }
    final = dict(page)
    final["moreAvailable"] = False
    client, _ = client_for(response(page, 1), response(final, 2))

    with pytest.raises(BetfairReadOnlyError, match="duplicate bet_id same"):
        client.read_all_current_orders(page_size=1)


def test_more_available_with_empty_page_fails_closed_instead_of_looping():
    client, transport = client_for(
        response({"currentOrders": [], "moreAvailable": True}, 1)
    )

    with pytest.raises(BetfairReadOnlyError, match="empty page"):
        client.read_all_current_orders(page_size=1000)

    assert len(transport.calls) == 1


def test_cleared_orders_use_settled_filter_and_parse_profit_as_signed_decimal():
    raw = response(
        {
            "clearedOrders": [
                {
                    "betId": "settled-1",
                    "marketId": "1.999",
                    "selectionId": 42,
                    "side": "BACK",
                    "placedDate": "2026-09-16T10:00:00+00:00",
                    "settledDate": "2026-09-17T10:00:00+00:00",
                    "priceRequested": 1.91,
                    "priceMatched": 1.90,
                    "sizeSettled": 10.00,
                    "profit": -10.00,
                }
            ],
            "moreAvailable": False,
        },
        1,
    )
    client, transport = client_for(raw)

    orders = client.read_all_cleared_orders(
        settled_from="2026-09-01T00:00:00+00:00",
        page_size=1000,
    )

    assert len(orders) == 1
    assert orders[0].bet_status == "SETTLED"
    assert orders[0].profit == Decimal("-10.0")
    assert orders[0].price_matched == Decimal("1.9")
    params = json.loads(transport.calls[0]["body"])["params"]
    assert params["betStatus"] == "SETTLED"
    assert params["groupBy"] == "BET"
    assert params["settledDateRange"] == {"from": "2026-09-01T00:00:00+00:00"}


def test_rpc_error_mismatched_id_malformed_json_and_missing_result_fail_closed():
    error = b'{"jsonrpc":"2.0","error":{"code":-32099,"message":"NO_SESSION"},"id":1}'
    client, _ = client_for(error)
    with pytest.raises(BetfairReadOnlyError, match="JSON-RPC returned an error"):
        client.read_account_funds()

    client, _ = client_for(b'{"jsonrpc":"2.0","result":{},"id":999}')
    with pytest.raises(BetfairReadOnlyError, match="id does not match"):
        client.read_account_details()

    client, _ = client_for(b"not-json")
    with pytest.raises(BetfairReadOnlyError, match="not valid UTF-8 JSON"):
        client.read_account_details()

    client, _ = client_for(b'{"jsonrpc":"2.0","id":1}')
    with pytest.raises(BetfairReadOnlyError, match="missing result"):
        client.read_account_details()


def test_write_rpc_names_are_outside_the_hard_allowlist():
    client, transport = client_for()

    for method in (
        "SportsAPING/v1.0/placeOrders",
        "SportsAPING/v1.0/cancelOrders",
        "SportsAPING/v1.0/replaceOrders",
        "SportsAPING/v1.0/updateOrders",
    ):
        with pytest.raises(BetfairReadOnlyError, match="read-only allowlist"):
            client._rpc(method, {})

    assert transport.calls == []
    assert not hasattr(client, "place_orders")
    assert not hasattr(client, "cancel_orders")
    assert not hasattr(client, "replace_orders")
    assert not hasattr(client, "update_orders")


def test_pagination_has_a_hard_bound():
    page = response({"currentOrders": [], "moreAvailable": True}, 1)
    client, _ = client_for(page)

    with pytest.raises(BetfairReadOnlyError):
        client.read_all_current_orders(max_pages=1)


def test_naive_clock_and_oversized_page_fail_closed_before_accepting_evidence():
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("a", "b"),
        transport=FakeTransport(
            [
                response(
                    {
                        "availableToBetBalance": 1,
                        "exposure": 0,
                        "retainedCommission": 0,
                        "exposureLimit": -10,
                    },
                    1,
                )
            ]
        ),
        clock=lambda: datetime(2026, 9, 17, 17, 30),
    )
    with pytest.raises(BetfairReadOnlyError, match="timezone-aware"):
        client.read_account_funds()

    client, _ = client_for()
    with pytest.raises(BetfairReadOnlyError, match="cannot exceed"):
        client.read_current_orders_page(record_count=1001)
