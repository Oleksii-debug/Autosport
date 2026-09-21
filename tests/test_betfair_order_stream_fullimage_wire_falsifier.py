from __future__ import annotations

from autosport.betfair_order_stream_readonly import (
    BetfairOrderStreamState,
    decode_order_change_message,
)


def test_official_ocm_sub_image_uses_runner_fullimage_without_market_img() -> None:
    """Accept the provider's OCM fullImage wire contract, not MCM's img key."""

    raw = {
        "op": "ocm",
        "initialClk": "initial-1",
        "clk": "clk-1",
        "pt": 1_790_000_000_100,
        "ct": "SUB_IMAGE",
        "oc": [
            {
                "id": "1.234",
                "orc": [
                    {
                        "fullImage": True,
                        "id": 42,
                        "hc": 0,
                        "uo": [
                            {
                                "id": "bet-1",
                                "p": 2.4,
                                "s": 10,
                                "side": "B",
                                "status": "E",
                                "pt": "L",
                                "ot": "L",
                                "pd": 1_790_000_000_000,
                                "sm": 0,
                                "sr": 10,
                                "sl": 0,
                                "sc": 0,
                                "sv": 0,
                                "rfo": "attempt-ref-1",
                                "rfs": "strategy-1",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    frame = decode_order_change_message(raw)
    state = BetfairOrderStreamState(subscription_sha256="a" * 64)
    result = state.apply(frame)

    assert result.cursor is not None
    assert result.cursor.initial_clk == "initial-1"
    order = state.state_for_bet_id("bet-1")
    assert order is not None
    assert order.customer_order_ref == "attempt-ref-1"
