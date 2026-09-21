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
from autosport.betfair_execution_fee_inputs import read_betfair_execution_fee_inputs


FIXED_NOW = datetime(2026, 9, 20, 23, 59, tzinfo=timezone.utc)


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


def client_for(*responses: bytes) -> tuple[BetfairReadOnlyClient, FakeTransport]:
    transport = FakeTransport(list(responses))
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
        venue_id="betfair-exchange",
        account_id="account-123",
    )
    return client, transport


def test_reads_authenticated_account_and_exact_market_fee_inputs_with_causal_evidence():
    account_raw = response(
        {
            "availableToBetBalance": 100.0,
            "exposure": 0,
            "retainedCommission": 0,
            "exposureLimit": -5000,
            "discountRate": 12.5,
        },
        1,
    )
    market_raw = response(
        [
            {
                "marketId": "1.234",
                "description": {
                    "marketBaseRate": 5.0,
                    "discountAllowed": True,
                },
            }
        ],
        2,
    )
    client, transport = client_for(account_raw, market_raw)

    observation = read_betfair_execution_fee_inputs(client, market_id="1.234")

    assert observation.venue_id == "betfair-exchange"
    assert observation.account_id == "account-123"
    assert observation.market_id == "1.234"
    assert observation.discount_rate_percent == Decimal("12.5")
    assert observation.market_base_rate_percent == Decimal("5.0")
    assert observation.discount_allowed is True
    assert observation.account_evidence.observed_at == FIXED_NOW.isoformat()
    assert observation.account_evidence.source_payload_sha256 == sha256(account_raw).hexdigest()
    assert observation.market_evidence.source_payload_sha256 == sha256(market_raw).hexdigest()

    assert [call["url"] for call in transport.calls] == [
        ACCOUNT_JSON_RPC_ENDPOINT,
        BETTING_JSON_RPC_ENDPOINT,
    ]
    requests = [json.loads(call["body"]) for call in transport.calls]
    assert requests[0]["method"] == "AccountAPING/v1.0/getAccountFunds"
    assert requests[0]["params"] == {}
    assert requests[1]["method"] == "SportsAPING/v1.0/listMarketCatalogue"
    assert requests[1]["params"] == {
        "filter": {"marketIds": ["1.234"]},
        "marketProjection": ["MARKET_DESCRIPTION"],
        "maxResults": 1,
    }


def test_missing_discount_rate_fails_closed_before_market_lookup():
    client, transport = client_for(
        response(
            {
                "availableToBetBalance": 100,
                "exposure": 0,
                "retainedCommission": 0,
                "exposureLimit": -5000,
            },
            1,
        )
    )

    with pytest.raises(
        BetfairReadOnlyError,
        match="discount_rate_percent is missing from provider response",
    ):
        read_betfair_execution_fee_inputs(client, market_id="1.234")

    assert len(transport.calls) == 1


@pytest.mark.parametrize("discount_rate", [-1, 101])
def test_out_of_range_provider_discount_rate_fails_closed(discount_rate: int):
    client, transport = client_for(
        response(
            {
                "availableToBetBalance": 100,
                "exposure": 0,
                "retainedCommission": 0,
                "exposureLimit": -5000,
                "discountRate": discount_rate,
            },
            1,
        )
    )

    with pytest.raises(BetfairReadOnlyError, match="between 0 and 100"):
        read_betfair_execution_fee_inputs(client, market_id="1.234")

    assert len(transport.calls) == 1


def test_market_lookup_requires_one_exact_matching_market():
    account = response(
        {
            "availableToBetBalance": 100,
            "exposure": 0,
            "retainedCommission": 0,
            "exposureLimit": -5000,
            "discountRate": 0,
        },
        1,
    )
    client, _ = client_for(
        account,
        response(
            [
                {
                    "marketId": "1.999",
                    "description": {
                        "marketBaseRate": 5,
                        "discountAllowed": True,
                    },
                }
            ],
            2,
        ),
    )

    with pytest.raises(BetfairReadOnlyError, match="different market"):
        read_betfair_execution_fee_inputs(client, market_id="1.234")


def test_market_description_missing_or_malformed_fee_fields_fail_closed():
    account = response(
        {
            "availableToBetBalance": 100,
            "exposure": 0,
            "retainedCommission": 0,
            "exposureLimit": -5000,
            "discountRate": 0,
        },
        1,
    )
    client, _ = client_for(
        account,
        response([{"marketId": "1.234", "description": {}}], 2),
    )
    with pytest.raises(
        BetfairReadOnlyError,
        match="market_base_rate_percent is missing from provider response",
    ):
        read_betfair_execution_fee_inputs(client, market_id="1.234")

    client, _ = client_for(
        account,
        response(
            [
                {
                    "marketId": "1.234",
                    "description": {
                        "marketBaseRate": 5,
                        "discountAllowed": "yes",
                    },
                }
            ],
            2,
        ),
    )
    with pytest.raises(BetfairReadOnlyError, match="discountAllowed must be bool"):
        read_betfair_execution_fee_inputs(client, market_id="1.234")
