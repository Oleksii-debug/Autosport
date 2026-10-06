from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json

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


def snapshot_evidence_hash(*payloads: bytes) -> str:
    payload_hashes = [sha256(payload).hexdigest() for payload in payloads]
    return sha256("|".join(payload_hashes).encode("ascii")).hexdigest()


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


def test_duplicate_json_object_key_fails_closed() -> None:
    with pytest.raises(BetfairReadOnlyError, match="duplicate object key"):
        _decode_json(b'{"jsonrpc":"2.0","result":{"id":1,"id":2}}')


def test_nonstandard_json_number_fails_closed() -> None:
    with pytest.raises(BetfairReadOnlyError, match="non-standard numeric constant"):
        _decode_json(b'{"jsonrpc":"2.0","result":{"value":NaN}}')


def test_account_snapshot_binds_canonical_balance_without_fabricated_total() -> None:
    transport = FakeTransport(
        [
            account_details(),
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


def test_empty_open_positions_page_is_bound_into_snapshot_evidence() -> None:
    details_payload = account_details()
    open_payload = rpc({"currentOrders": [], "moreAvailable": False}, 2)
    transport = FakeTransport([details_payload, open_payload])
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("application", "session"),
        transport=transport,
        clock=lambda: NOW,
    )

    snapshot = client.read_account_snapshot(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ})
    )

    expected = snapshot_evidence_hash(details_payload, open_payload)
    assert snapshot.open_positions == ()
    assert snapshot.profile.source_payload_sha256 == expected
    assert snapshot.profile.source_ref == f"betfair://account-snapshot/{expected}"
    assert transport.responses == []


def test_empty_settled_positions_page_is_bound_into_snapshot_evidence() -> None:
    details_payload = account_details()
    settled_payload = rpc({"clearedOrders": [], "moreAvailable": False}, 2)
    transport = FakeTransport([details_payload, settled_payload])
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("application", "session"),
        transport=transport,
        clock=lambda: NOW,
    )

    snapshot = client.read_account_snapshot(
        frozenset({BookmakerCapability.SETTLED_POSITIONS_READ})
    )

    expected = snapshot_evidence_hash(details_payload, settled_payload)
    assert snapshot.settled_positions == ()
    assert snapshot.profile.source_payload_sha256 == expected
    assert snapshot.profile.source_ref == f"betfair://account-snapshot/{expected}"
    assert transport.responses == []


def test_empty_open_payload_change_changes_snapshot_evidence_identity() -> None:
    details_payload = account_details()
    first_open_payload = rpc({"currentOrders": [], "moreAvailable": False}, 2)
    second_open_payload = rpc(
        {"currentOrders": [], "moreAvailable": False, "providerMarker": "changed"},
        2,
    )

    first_client = BetfairReadOnlyClient(
        BetfairSessionCredentials("application", "session"),
        transport=FakeTransport([details_payload, first_open_payload]),
        clock=lambda: NOW,
    )
    second_client = BetfairReadOnlyClient(
        BetfairSessionCredentials("application", "session"),
        transport=FakeTransport([details_payload, second_open_payload]),
        clock=lambda: NOW,
    )

    first_snapshot = first_client.read_account_snapshot(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ})
    )
    second_snapshot = second_client.read_account_snapshot(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ})
    )

    assert first_snapshot.open_positions == second_snapshot.open_positions == ()
    assert first_snapshot.profile.source_payload_sha256 == snapshot_evidence_hash(
        details_payload, first_open_payload
    )
    assert second_snapshot.profile.source_payload_sha256 == snapshot_evidence_hash(
        details_payload, second_open_payload
    )
    assert (
        first_snapshot.profile.source_payload_sha256
        != second_snapshot.profile.source_payload_sha256
    )


def test_terminal_empty_open_page_remains_bound_once_in_page_order() -> None:
    details_payload = account_details()
    first_page_payload = rpc(
        {
            "currentOrders": [
                {
                    "betId": "bet-1",
                    "marketId": "1.234",
                    "selectionId": 123,
                    "side": "BACK",
                    "status": "EXECUTABLE",
                    "placedDate": "2026-09-17T18:40:00+00:00",
                    "priceSize": {"price": 2.0, "size": 10.0},
                    "averagePriceMatched": 0.0,
                    "sizeMatched": 0.0,
                    "sizeRemaining": 10.0,
                }
            ],
            "moreAvailable": True,
        },
        2,
    )
    terminal_page_payload = rpc({"currentOrders": [], "moreAvailable": False}, 3)
    transport = FakeTransport(
        [details_payload, first_page_payload, terminal_page_payload]
    )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("application", "session"),
        transport=transport,
        clock=lambda: NOW,
    )

    snapshot = client.read_account_snapshot(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ})
    )

    expected = snapshot_evidence_hash(
        details_payload, first_page_payload, terminal_page_payload
    )
    assert snapshot.open_positions == ()
    assert snapshot.profile.source_payload_sha256 == expected
    assert snapshot.profile.source_ref == f"betfair://account-snapshot/{expected}"
    assert transport.responses == []
