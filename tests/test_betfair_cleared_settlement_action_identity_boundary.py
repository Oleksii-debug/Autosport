from datetime import datetime, timezone
import json

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_cleared_settlement_evidence import (
    resolve_betfair_cleared_bet_settlement,
)


FIXED_NOW = datetime(2026, 9, 21, 10, 30, tzinfo=timezone.utc)
ORDER_REF = "abc123"
MARKET_ID = "1.234"
EVENT_ID = "event-1"
BET_ID = "bet-1"


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)

    def post(
        self,
        _url: str,
        *,
        headers: object,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        del headers, body, timeout_seconds
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def test_direct_generic_readback_cannot_bind_forged_local_action() -> None:
    settled_row = {
        "betId": BET_ID,
        "marketId": MARKET_ID,
        "selectionId": 42,
        "side": "BACK",
        "placedDate": "2026-09-21T08:00:00+00:00",
        "settledDate": "2026-09-21T09:00:00+00:00",
        "priceRequested": 2.0,
        "priceMatched": 2.1,
        "sizeSettled": 10.0,
        "profit": 11.0,
        "customerOrderRef": ORDER_REF,
        "eventId": EVENT_ID,
    }
    responses = [
        _response(
            [{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}],
            1,
        ),
        _response({"currentOrders": [], "moreAvailable": False}, 2),
        _response(
            {"clearedOrders": [settled_row], "moreAvailable": False},
            3,
        ),
        _response({"clearedOrders": [], "moreAvailable": False}, 4),
        _response({"clearedOrders": [], "moreAvailable": False}, 5),
        _response({"clearedOrders": [], "moreAvailable": False}, 6),
    ]
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=FakeTransport(responses),
        clock=lambda: FIXED_NOW,
    )
    readback = client.read_execution_readback(
        action_id="forged-local-action",
        market_id=MARKET_ID,
        provider_order_ref=ORDER_REF,
    )

    evidence = resolve_betfair_cleared_bet_settlement(readback)

    assert evidence is not None
    evidence.assert_authoritative()
    assert evidence.provider_order_ref == ORDER_REF
    assert evidence.bet_id == BET_ID
    assert evidence.action_id is None
