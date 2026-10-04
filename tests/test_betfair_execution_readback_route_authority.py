from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)


NOW = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
TARGET_REF = "a" * 32


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


class _RouteMutatingTransport:
    def __init__(
        self,
        responses: list[bytes],
        *,
        attribute: str,
        replacement: str,
    ) -> None:
        self.responses = list(responses)
        self.attribute = attribute
        self.replacement = replacement
        self.client: BetfairReadOnlyClient | None = None
        self.mutated = False

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        del url, headers, timeout_seconds
        request = json.loads(body.decode("utf-8"))
        if (
            not self.mutated
            and request["method"] == "SportsAPING/v1.0/listCurrentOrders"
        ):
            assert self.client is not None
            setattr(self.client, self.attribute, self.replacement)
            self.mutated = True
        if not self.responses:
            raise AssertionError("unexpected provider call")
        return self.responses.pop(0)


@pytest.mark.parametrize(
    ("attribute", "replacement"),
    [
        ("_account_id", "acct-poisoned"),
        ("_venue_id", "betfair-poisoned"),
    ],
)
def test_execution_readback_rejects_route_drift_during_provider_io(
    attribute: str,
    replacement: str,
) -> None:
    # Exact-ref all-empty capture performs one market lookup and two complete
    # CURRENT/SETTLED/VOIDED/LAPSED/CANCELLED sweeps. The transport mutation is
    # intentionally structural/non-authoritative: route drift must be rejected by
    # the readback boundary itself before any later origin capability can exist.
    responses = [
        _response([{"marketId": "1.234", "event": {"id": "event-1"}}], 1),
        _response({"currentOrders": [], "moreAvailable": False}, 2),
        _response({"clearedOrders": [], "moreAvailable": False}, 3),
        _response({"clearedOrders": [], "moreAvailable": False}, 4),
        _response({"clearedOrders": [], "moreAvailable": False}, 5),
        _response({"clearedOrders": [], "moreAvailable": False}, 6),
        _response({"currentOrders": [], "moreAvailable": False}, 7),
        _response({"clearedOrders": [], "moreAvailable": False}, 8),
        _response({"clearedOrders": [], "moreAvailable": False}, 9),
        _response({"clearedOrders": [], "moreAvailable": False}, 10),
        _response({"clearedOrders": [], "moreAvailable": False}, 11),
    ]
    transport = _RouteMutatingTransport(
        responses,
        attribute=attribute,
        replacement=replacement,
    )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=lambda: NOW,
        account_id="acct-1",
    )
    transport.client = client

    with pytest.raises(
        BetfairReadOnlyError,
        match="configured account route changed during execution readback",
    ):
        client.read_execution_readback(
            action_id="action-1",
            market_id="1.234",
            provider_order_ref=TARGET_REF,
        )

    assert transport.mutated is True
