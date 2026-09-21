from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

import autosport.betfair_execution_fee_inputs as fee_inputs
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)


FIXED_NOW = datetime(2026, 9, 21, 0, 40, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, url, *, headers, body: bytes, timeout_seconds: float) -> bytes:
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


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def test_preentry_module_and_matching_class_rebinding_cannot_replace_executables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeTransport(
        [
            _response(
                {
                    "currencyCode": "GBP",
                    "region": "GBR",
                    "discountRate": 12.5,
                },
                1,
            ),
            _response(
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
            ),
        ]
    )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
        venue_id="betfair-exchange",
        account_id="account-123",
    )
    forged_calls: list[str] = []

    def forged(*args, **kwargs):
        forged_calls.append("forged")
        raise AssertionError("pre-entry replacement must never execute")

    # Rebind the old module mirrors (even though source no longer trusts them), the
    # live class descriptors, and the implementation globals to the same callable.
    # The exported partial already owns the original snapshot/RPC/helper objects.
    for name in (
        "_CANONICAL_RPC",
        "_CANONICAL_NEXT_REQUEST_ID",
        "_CANONICAL_OBSERVED_AT",
        "_CANONICAL_REDACT_PROVIDER_MESSAGE",
    ):
        monkeypatch.setattr(fee_inputs, name, forged, raising=False)
    monkeypatch.setattr(fee_inputs, "_snapshot_canonical_client", forged)
    monkeypatch.setattr(fee_inputs, "_read_betfair_execution_fee_inputs", forged)
    for name in (
        "_rpc",
        "_next_request_id",
        "_observed_at",
        "_redact_provider_message",
    ):
        monkeypatch.setattr(BetfairReadOnlyClient, name, forged)

    observation = fee_inputs.read_betfair_execution_fee_inputs(
        client,
        market_id="1.234",
    )

    assert forged_calls == []
    assert observation.venue_id == "betfair-exchange"
    assert observation.account_id == "account-123"
    assert observation.discount_rate_percent == Decimal("12.5")
    assert observation.market_base_rate_percent == Decimal("5.0")
    assert observation.regulator == "MR_INT"
    assert [json.loads(call["body"])["id"] for call in transport.calls] == [1, 2]


def test_public_executable_binding_slots_are_read_only() -> None:
    reader = fee_inputs.read_betfair_execution_fee_inputs

    with pytest.raises(AttributeError, match="readonly attribute"):
        reader.func = lambda *args, **kwargs: None
    with pytest.raises(AttributeError, match="readonly attribute"):
        reader.args = ()
