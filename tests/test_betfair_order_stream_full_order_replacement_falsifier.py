from __future__ import annotations

import pytest

from autosport.betfair_order_stream_readonly import (
    BetfairOrderStreamState,
    decode_order_change_message,
)


SUBSCRIPTION_SHA256 = "a" * 64


def _full_limit_order(*, status: str, size_matched: int, size_remaining: int) -> dict[str, object]:
    return {
        "id": "bet-1",
        "p": 2,
        "s": 10,
        "side": "B",
        "status": status,
        "pt": "L",
        "ot": "L",
        "pd": 1_700_000_000_000,
        "sm": size_matched,
        "sr": size_remaining,
        "sl": 0,
        "sc": 0,
        "sv": 0,
    }


def _frame(
    *,
    clk: str,
    publish_time_ms: int,
    order: dict[str, object],
    change_type: str | None = None,
) -> dict[str, object]:
    raw: dict[str, object] = {
        "op": "ocm",
        "clk": clk,
        "pt": publish_time_ms,
        "oc": [
            {
                "id": "1.234",
                "orc": [
                    {
                        "id": 123,
                        "uo": [order],
                    }
                ],
            }
        ],
    }
    if change_type is not None:
        raw["ct"] = change_type
    if change_type == "SUB_IMAGE":
        raw["initialClk"] = "initial-1"
        # Parent #1015 currently uses the MCM-style img key. Keep this
        # falsifier isolated from the independent fullImage wire-contract child.
        market = raw["oc"][0]  # type: ignore[index]
        market["img"] = True  # type: ignore[index]
    return raw


def test_changed_order_must_not_inherit_omitted_full_order_fields() -> None:
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)
    initial = _frame(
        clk="clk-1",
        publish_time_ms=100,
        change_type="SUB_IMAGE",
        order=_full_limit_order(status="E", size_matched=0, size_remaining=10),
    )
    state.apply(decode_order_change_message(initial))
    before_snapshot = state.snapshot()
    before_cursor = state.reconnect_cursor()

    # Betfair documents that every uo order change is sent in full and that
    # order-cache handling replaces the order by betId. Deliberately omit sr
    # from the next LIMIT order change. Accepting it and inheriting sr=10 from
    # the previous E state would synthesize an impossible EC state with
    # s=10, sm=10, and stale sr=10.
    partial = _full_limit_order(status="EC", size_matched=10, size_remaining=0)
    del partial["sr"]

    with pytest.raises(ValueError):
        state.apply(
            decode_order_change_message(
                _frame(
                    clk="clk-2",
                    publish_time_ms=200,
                    order=partial,
                )
            )
        )

    assert state.snapshot() == before_snapshot
    assert state.reconnect_cursor() == before_cursor
