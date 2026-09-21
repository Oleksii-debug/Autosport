from __future__ import annotations

from decimal import Decimal

from autosport.betfair_order_stream_readonly import decode_order_change_message


def test_ocm_preserves_line_market_position_at_or_below_one() -> None:
    """Order Stream uo.p is not universally a decimal-odds domain.

    Betfair documents that for LINE markets the same field carries the line
    position. The OCM payload itself does not carry the market betting type, so
    the read-only codec must preserve a finite numeric value instead of
    rejecting it solely because it is <= 1. Market-definition-aware validation
    belongs at the later join with canonical market metadata.
    """

    raw = {
        "op": "ocm",
        "clk": "clk-line-1",
        "pt": 1_790_000_000_100,
        "oc": [
            {
                "id": "1.line-market",
                "orc": [
                    {
                        "id": 42,
                        "hc": 0,
                        "uo": [
                            {
                                "id": "bet-line-1",
                                "p": 0.5,
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
                                "rfo": "line-ref-1",
                                "rfs": "strategy-1",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    frame = decode_order_change_message(raw)
    order = frame.market_changes[0].runner_changes[0].unmatched_orders[0]

    assert order.price == Decimal("0.5")
