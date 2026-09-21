from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from threading import Event

from autosport import betfair_execution_fee_inputs as fee_inputs
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)


FIXED_NOW = datetime(2026, 9, 21, 0, 27, tzinfo=timezone.utc)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def test_first_provider_callback_cannot_rebind_whole_operation_rpc_capability() -> None:
    account_raw = _response(
        {
            "currencyCode": "GBP",
            "region": "GBR",
            "discountRate": 12.5,
        },
        1,
    )
    market_raw = _response(
        [
            {
                "marketId": "1.234",
                "description": {
                    "marketBaseRate": 5.0,
                    "discountAllowed": True,
                    "regulator": "MR_INT",
                },
            }
        ],
        2,
    )
    forged_called = Event()
    original_rpc = fee_inputs._CANONICAL_RPC

    def forged_rpc(*args, **kwargs):
        forged_called.set()
        raise AssertionError("provider callback module rebind must not redirect second read")

    class RebindingTransport:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, url, *, headers, body, timeout_seconds):
            self.calls += 1
            if self.calls == 1:
                fee_inputs._CANONICAL_RPC = forged_rpc
                return account_raw
            return market_raw

    transport = RebindingTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
        venue_id="betfair-exchange",
        account_id="account-123",
    )

    try:
        observation = fee_inputs.read_betfair_execution_fee_inputs(
            client,
            market_id="1.234",
        )
    finally:
        fee_inputs._CANONICAL_RPC = original_rpc

    assert not forged_called.is_set()
    assert transport.calls == 2
    assert observation.discount_rate_percent == Decimal("12.5")
    assert observation.market_base_rate_percent == Decimal("5.0")
    assert observation.regulator == "MR_INT"
    assert observation.market_evidence.source_payload_sha256 == sha256(market_raw).hexdigest()
