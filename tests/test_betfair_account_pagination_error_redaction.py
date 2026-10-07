from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)


class _FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self._responses = list(responses)

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        del url, headers, body, timeout_seconds
        if not self._responses:
            raise AssertionError("unexpected transport call")
        return self._responses.pop(0)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _order(bet_id: str) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": "1.234",
        "selectionId": 17,
        "side": "BACK",
        "status": "EXECUTABLE",
        "placedDate": "2026-09-17T20:00:00+00:00",
        "priceSize": {"price": 2, "size": 1},
        "averagePriceMatched": 0,
        "sizeMatched": 0,
        "sizeRemaining": 1,
    }


def test_cross_page_duplicate_bet_id_is_not_reflected_in_diagnostic() -> None:
    provider_controlled_bet_id = "session-secret"
    first = _response(
        {"currentOrders": [_order(provider_controlled_bet_id)], "moreAvailable": True},
        1,
    )
    second = _response(
        {"currentOrders": [_order(provider_controlled_bet_id)], "moreAvailable": False},
        2,
    )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=_FakeTransport([first, second]),
        clock=lambda: datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(BetfairReadOnlyError) as caught:
        client.read_all_current_orders(page_size=1)

    diagnostic = str(caught.value)
    assert diagnostic == "currentOrders pagination returned duplicate bet_id"
    assert provider_controlled_bet_id not in diagnostic
    assert "app-secret" not in diagnostic
