from __future__ import annotations

from datetime import datetime, timezone
import json
from decimal import Decimal

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
    _decode_json,
)
from autosport.bookmaker_capability import BookmakerCapability


NOW = datetime(2026, 9, 17, 18, 45, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        assert url.startswith("https://api.betfair.com/exchange/")
        assert headers["X-Authentication"] == "session"
        assert headers["X-Application"] == "application"
        assert b"session" not in body
        return self.responses.pop(0)


def rpc(result: object, request_id: int) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "result": result, "id": request_id}).encode()


def test_duplicate_json_object_key_fails_closed() -> None:
    with pytest.raises(BetfairReadOnlyError, match="duplicate object key"):
        _decode_json(b'{"jsonrpc":"2.0","result":{"id":1,"id":2}}')


def test_nonstandard_json_number_fails_closed() -> None:
    with pytest.raises(BetfairReadOnlyError, match="non-standard numeric constant"):
        _decode_json(b'{"jsonrpc":"2.0","result":{"value":NaN}}')


def test_account_snapshot_binds_canonical_balance_without_fabricated_total() -> None:
    transport = FakeTransport(
        [
            rpc({"currencyCode": "EUR", "localeCode": "en", "region": "SK", "timezone": "Europe/Bratislava"}, 1),
            rpc({"availableToBetBalance": 123.45, "exposure": -2.5, "retainedCommission": 0.25, "exposureLimit": -1000}, 2),
        ]
    )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("application", "session"),
        transport=transport,
        clock=lambda: NOW,
    )

    snapshot = client.read_account_snapshot(frozenset({BookmakerCapability.BALANCE_READ}))

    assert snapshot.profile.venue_id == "betfair"
    assert snapshot.balance is not None
    assert snapshot.balance.currency == "EUR"
    assert snapshot.balance.available_balance == Decimal("123.45")
    assert snapshot.balance.total_balance is None
    assert snapshot.balance.exposure == Decimal("-2.5")
    assert snapshot.observed_capabilities == frozenset({BookmakerCapability.BALANCE_READ})
