from __future__ import annotations

from autosport.betfair_order_stream_readonly import decode_order_change_message


def test_ocm_empty_customer_references_are_absent_not_shared_identity() -> None:
    """Provider-valid empty rfo/rfs must not become one synthetic identity."""

    raw = {
        "op": "ocm",
        "clk": "clk-empty-refs-1",
        "pt": 1_790_000_000_100,
        "oc": [
            {
                "id": "1.234",
                "orc": [
                    {
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
                                "rfo": "",
                                "rfs": "",
                            },
                            {
                                "id": "bet-2",
                                "p": 2.6,
                                "s": 5,
                                "side": "L",
                                "status": "E",
                                "pt": "L",
                                "ot": "L",
                                "pd": 1_790_000_000_001,
                                "sm": 0,
                                "sr": 5,
                                "sl": 0,
                                "sc": 0,
                                "sv": 0,
                                "rfo": "",
                                "rfs": "",
                            },
                        ],
                    }
                ],
            }
        ],
    }

    frame = decode_order_change_message(raw)
    orders = frame.market_changes[0].runner_changes[0].unmatched_orders

    assert len(orders) == 2
    assert all(order.customer_order_ref is None for order in orders)
    assert all(order.customer_strategy_ref is None for order in orders)
