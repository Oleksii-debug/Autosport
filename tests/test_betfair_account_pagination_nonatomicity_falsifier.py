"""Falsify silent loss from mutable offset pagination in Betfair account reads.

The provider's listCurrentOrders pagination is offset-based. If the ordered result set
changes between successful pages, a row can shift to an index lower than the next
fromRecord and be skipped without duplicate IDs, an empty-moreAvailable anomaly, or
an RPC failure.

A safe account-read path may fail closed on the incoherent sweep or perform a bounded
coherence reread. It must not publish an apparently complete account snapshot that
silently omits the surviving open position.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.bookmaker_capability import BookmakerCapability


OBSERVED_AT = datetime(2026, 9, 21, 18, 58, tzinfo=timezone.utc)


def _rpc_response(request: dict[str, object], result: object) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": result,
            "id": request["id"],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _current_order(bet_id: str) -> dict[str, object]:
    return {
        "betId": bet_id,
        "marketId": "1.234",
        "selectionId": 42,
        "side": "BACK",
        "status": "EXECUTABLE",
        "placedDate": "2026-09-21T18:50:00+00:00",
        "priceSize": {"price": 2.0, "size": 10.0},
        "averagePriceMatched": 2.0,
        "sizeMatched": 10.0,
        "sizeRemaining": 0.0,
        "customerOrderRef": f"ref-{bet_id}",
    }


class _ShiftingCurrentOrdersTransport:
    """Successful provider responses whose current-order set changes between pages."""

    def __init__(self) -> None:
        self.current_calls: list[int] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body.decode("utf-8"))
        method = request["method"]

        if method.endswith("getAccountDetails"):
            return _rpc_response(
                request,
                {
                    "currencyCode": "GBP",
                    "localeCode": "en",
                    "region": "GBR",
                    "timezone": "Europe/London",
                },
            )

        if not method.endswith("listCurrentOrders"):
            raise AssertionError(f"unexpected provider method: {method}")

        params = request["params"]
        offset = params["fromRecord"]
        self.current_calls.append(offset)

        if len(self.current_calls) == 1:
            # Provider state at page 1: [bet-a, bet-b]. Only the first row is
            # returned and moreAvailable correctly says another row exists.
            assert offset == 0
            return _rpc_response(
                request,
                {
                    "currentOrders": [_current_order("bet-a")],
                    "moreAvailable": True,
                },
            )

        if len(self.current_calls) == 2:
            # bet-a has now left currentOrders. Provider state is [bet-b].
            # Offset 1 is therefore past the surviving row. This is a fully
            # successful response, so error-only degradation handling cannot
            # detect the omission.
            assert offset == 1
            return _rpc_response(
                request,
                {
                    "currentOrders": [],
                    "moreAvailable": False,
                },
            )

        # A bounded coherence reread from zero can recover the current state.
        assert offset == 0
        return _rpc_response(
            request,
            {
                "currentOrders": [_current_order("bet-b")],
                "moreAvailable": False,
            },
        )


def test_successful_offset_shift_cannot_silently_omit_open_position() -> None:
    transport = _ShiftingCurrentOrdersTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=lambda: OBSERVED_AT,
        venue_id="betfair",
        account_id="acct-1",
    )

    try:
        snapshot = client.read_account_snapshot(
            frozenset({BookmakerCapability.OPEN_POSITIONS_READ})
        )
    except BetfairReadOnlyError:
        # Explicit incompleteness/conflict is a safe outcome.
        return

    open_ids = {
        position.external_position_id
        for position in snapshot.open_positions
    }

    # Returning only bet-a is unsafe: bet-b is still open in the provider's
    # later state and was skipped solely because the offset was applied to a
    # mutated result set. A coherent reread may return bet-b (or conservatively
    # retain additional exposure), but it must not omit bet-b.
    assert "bet-b" in open_ids
