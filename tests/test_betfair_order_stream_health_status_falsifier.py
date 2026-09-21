from __future__ import annotations

from autosport.betfair_order_stream_readonly import (
    BetfairOrderStreamState,
    decode_order_change_message,
)
from autosport.betfair_stream_codec import BetfairApplyStatus


SUBSCRIPTION_SHA256 = "a" * 64
_TRUSTED_STATUSES = {
    BetfairApplyStatus.APPLIED,
    BetfairApplyStatus.HEARTBEAT,
    BetfairApplyStatus.DUPLICATE,
}


def _initialized_state() -> BetfairOrderStreamState:
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)
    state.apply(
        decode_order_change_message(
            {
                "op": "ocm",
                "ct": "SUB_IMAGE",
                "initialClk": "initial-1",
                "clk": "clk-1",
                "pt": 100,
                "oc": [],
            }
        )
    )
    return state


def _apply_provider_degraded(
    state: BetfairOrderStreamState,
    raw: dict[str, object],
):
    try:
        return state.apply(decode_order_change_message(raw))
    except ValueError:
        return None


def test_provider_status_503_delta_cannot_advance_trusted_order_state() -> None:
    state = _initialized_state()
    before_snapshot = state.snapshot()
    before_cursor = state.reconnect_cursor()

    result = _apply_provider_degraded(
        state,
        {
            "op": "ocm",
            "clk": "clk-2",
            "pt": 200,
            "status": 503,
            "oc": [
                {
                    "id": "1.234",
                    "orc": [
                        {
                            "id": 42,
                            "uo": [
                                {
                                    "id": "bet-degraded",
                                    "side": "B",
                                    "status": "E",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )

    if result is not None:
        assert result.status not in _TRUSTED_STATUSES
    assert state.snapshot() == before_snapshot
    assert state.reconnect_cursor() == before_cursor
    assert state.state_for_bet_id("bet-degraded") is None


def test_provider_status_503_heartbeat_cannot_advance_trusted_cursor() -> None:
    state = _initialized_state()
    before_snapshot = state.snapshot()
    before_cursor = state.reconnect_cursor()

    result = _apply_provider_degraded(
        state,
        {
            "op": "ocm",
            "ct": "HEARTBEAT",
            "clk": "clk-2",
            "pt": 200,
            "status": 503,
            "oc": [],
        },
    )

    if result is not None:
        assert result.status not in _TRUSTED_STATUSES
    assert state.snapshot() == before_snapshot
    assert state.reconnect_cursor() == before_cursor
