from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autosport.betfair_order_stream_readonly import (
    ORDER_STREAM_FINAL_SETTLEMENT_AUTHORITY,
    BetfairOrderStreamState,
    decode_order_change_message,
)
from autosport.betfair_stream_codec import (
    BetfairApplyStatus,
    BetfairCrlfJsonDecoder,
    BetfairFrameKind,
)


SUBSCRIPTION_SHA256 = "a" * 64


def _order(
    *,
    bet_id: str = "bet-1",
    customer_order_ref: str = "attempt-ref-1",
    status: str = "E",
    average_price_matched: float = 0,
    size_matched: float = 0,
    size_remaining: float = 10,
) -> dict[str, object]:
    return {
        "id": bet_id,
        "p": 2.4,
        "s": 10,
        "side": "B",
        "status": status,
        "pt": "L",
        "ot": "L",
        "pd": 1_790_000_000_000,
        "avp": average_price_matched,
        "sm": size_matched,
        "sr": size_remaining,
        "sl": 0,
        "sc": 0,
        "sv": 0,
        "rfo": customer_order_ref,
        "rfs": "strategy-1",
    }


def _frame(
    *,
    clk: str,
    publish_time_ms: int,
    change_type: str | None = None,
    initial_clk: str | None = None,
    orders: list[dict[str, object]] | None = None,
    market_id: str = "1.234",
    image: bool = False,
    include_market: bool = True,
) -> dict[str, object]:
    frame: dict[str, object] = {
        "op": "ocm",
        "clk": clk,
        "pt": publish_time_ms,
    }
    if change_type is not None:
        frame["ct"] = change_type
    if initial_clk is not None:
        frame["initialClk"] = initial_clk
    if include_market:
        frame["oc"] = [
            {
                "id": market_id,
                "img": image,
                "orc": [
                    {
                        "id": 42,
                        "hc": 0,
                        "uo": [] if orders is None else orders,
                    }
                ],
            }
        ]
    else:
        frame["oc"] = []
    return frame


def _decoded(raw: dict[str, object]):
    payload = json.dumps(raw, separators=(",", ":")).encode("utf-8") + b"\r\n"
    decoder = BetfairCrlfJsonDecoder()
    decoded = decoder.feed(payload)
    decoder.finish()
    assert len(decoded) == 1
    return decode_order_change_message(decoded[0])


def _initialized_state() -> BetfairOrderStreamState:
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)
    state.apply(
        _decoded(
            _frame(
                clk="clk-1",
                initial_clk="initial-1",
                publish_time_ms=100,
                change_type="SUB_IMAGE",
                image=True,
                orders=[_order()],
            )
        )
    )
    return state


def test_ocm_uses_shared_strict_framing_and_builds_subscription_bound_cursor() -> None:
    raw = _frame(
        clk="clk-1",
        initial_clk="initial-1",
        publish_time_ms=100,
        change_type="SUB_IMAGE",
        image=True,
        orders=[_order()],
    )
    frame = _decoded(raw)
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)

    result = state.apply(frame)

    assert result.status is BetfairApplyStatus.APPLIED
    assert result.frame_kind is BetfairFrameKind.SUB_IMAGE
    assert result.cursor is not None
    assert result.cursor.subscription_sha256 == SUBSCRIPTION_SHA256
    assert result.cursor.initial_clk == "initial-1"
    assert result.cursor.clk == "clk-1"
    order = state.state_for_bet_id("bet-1")
    assert order is not None
    assert order.customer_order_ref == "attempt-ref-1"
    assert state.bet_id_for_customer_order_ref("attempt-ref-1") == "bet-1"


def test_customer_order_ref_binds_provider_bet_id_once_even_after_image_removal() -> None:
    state = _initialized_state()
    replacement = _frame(
        clk="clk-2",
        publish_time_ms=200,
        orders=[],
        image=True,
    )
    result = state.apply(_decoded(replacement))
    assert {item.bet_id for item in result.removed} == {"bet-1"}
    assert state.state_for_bet_id("bet-1") is None

    conflicting = _frame(
        clk="clk-3",
        publish_time_ms=300,
        orders=[_order(bet_id="bet-2", customer_order_ref="attempt-ref-1")],
    )
    with pytest.raises(ValueError, match="customerOrderRef rebound"):
        state.apply(_decoded(conflicting))


def test_bet_id_cannot_rebind_to_another_customer_order_ref() -> None:
    state = _initialized_state()
    conflicting = _frame(
        clk="clk-2",
        publish_time_ms=200,
        orders=[_order(bet_id="bet-1", customer_order_ref="different-ref")],
    )

    with pytest.raises(ValueError, match="betId rebound"):
        state.apply(_decoded(conflicting))


def test_resub_delta_replay_is_idempotent_and_absolute_not_additive() -> None:
    state = _initialized_state()
    raw = _frame(
        clk="clk-2",
        initial_clk="initial-1",
        publish_time_ms=200,
        change_type="RESUB_DELTA",
        orders=[
            _order(
                status="EC",
                average_price_matched=2.5,
                size_matched=4,
                size_remaining=6,
            )
        ],
    )
    frame = _decoded(raw)

    first = state.apply(frame)
    second = state.apply(frame)

    assert first.status is BetfairApplyStatus.APPLIED
    assert second.status is BetfairApplyStatus.DUPLICATE
    order = state.state_for_bet_id("bet-1")
    assert order is not None
    assert order.size_matched == Decimal("4")
    assert order.size_remaining == Decimal("6")
    assert order.average_price_matched == Decimal("2.5")
    assert order.accepted_average_price == Decimal("2.5")


def test_market_image_replaces_current_cache_without_erasing_identity_history() -> None:
    state = _initialized_state()
    second_market = _frame(
        clk="clk-2",
        publish_time_ms=200,
        market_id="1.999",
        orders=[_order(bet_id="bet-2", customer_order_ref="attempt-ref-2")],
    )
    state.apply(_decoded(second_market))
    assert {item.identity.bet_id for item in state.snapshot()} == {"bet-1", "bet-2"}

    replacement = _frame(
        clk="clk-3",
        publish_time_ms=300,
        market_id="1.234",
        image=True,
        orders=[_order(bet_id="bet-3", customer_order_ref="attempt-ref-3")],
    )
    result = state.apply(_decoded(replacement))

    assert result.image_replaced_markets == ("1.234",)
    assert {item.bet_id for item in result.removed} == {"bet-1"}
    assert {item.identity.bet_id for item in state.snapshot()} == {"bet-2", "bet-3"}
    assert state.bet_id_for_customer_order_ref("attempt-ref-1") == "bet-1"


def test_rejected_multi_order_frame_rolls_back_cache_identity_and_cursor() -> None:
    state = _initialized_state()
    before_snapshot = state.snapshot()
    before_cursor = state.reconnect_cursor()
    rejected = _frame(
        clk="clk-2",
        publish_time_ms=200,
        orders=[
            _order(bet_id="bet-2", customer_order_ref="attempt-ref-2"),
            _order(bet_id="bet-3", customer_order_ref="attempt-ref-1"),
        ],
    )

    with pytest.raises(ValueError, match="customerOrderRef rebound"):
        state.apply(_decoded(rejected))

    assert state.snapshot() == before_snapshot
    assert state.reconnect_cursor() == before_cursor
    assert state.state_for_bet_id("bet-2") is None
    assert state.bet_id_for_customer_order_ref("attempt-ref-2") is None
    assert state.bet_id_for_customer_order_ref("attempt-ref-1") == "bet-1"


def test_delta_before_subscription_image_fails_closed() -> None:
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)
    frame = _decoded(
        _frame(clk="clk-1", publish_time_ms=100, orders=[_order()])
    )

    with pytest.raises(ValueError, match="before SUB_IMAGE"):
        state.apply(frame)


def test_same_clock_with_different_content_fails_closed() -> None:
    state = _initialized_state()
    first = _frame(clk="clk-2", publish_time_ms=200, orders=[_order(status="EC")])
    state.apply(_decoded(first))
    different = _frame(
        clk="clk-2",
        publish_time_ms=200,
        orders=[_order(status="EC", size_matched=1, size_remaining=9)],
    )

    with pytest.raises(ValueError, match="same Betfair order-stream clk"):
        state.apply(_decoded(different))


def test_resume_initial_clock_conflict_fails_closed() -> None:
    state = _initialized_state()
    frame = _decoded(
        _frame(
            clk="clk-2",
            initial_clk="other-initial",
            publish_time_ms=200,
            change_type="RESUB_DELTA",
            orders=[_order()],
        )
    )

    with pytest.raises(ValueError, match="initialClk conflicts"):
        state.apply(frame)


def test_publish_time_cannot_move_backwards() -> None:
    state = _initialized_state()
    frame = _decoded(
        _frame(clk="clk-2", publish_time_ms=99, orders=[_order()])
    )

    with pytest.raises(ValueError, match="publish time moved backwards"):
        state.apply(frame)


def test_execution_complete_order_cannot_resurrect_to_executable() -> None:
    state = _initialized_state()
    state.apply(
        _decoded(
            _frame(
                clk="clk-2",
                publish_time_ms=200,
                orders=[_order(status="EC", size_matched=10, size_remaining=0)],
            )
        )
    )
    resurrected = _decoded(
        _frame(clk="clk-3", publish_time_ms=300, orders=[_order(status="E")])
    )

    with pytest.raises(ValueError, match="cannot become executable"):
        state.apply(resurrected)


def test_average_price_is_not_accepted_evidence_without_positive_matched_size() -> None:
    state = _initialized_state()
    zero_match = state.state_for_bet_id("bet-1")
    assert zero_match is not None
    assert zero_match.accepted_average_price is None

    state.apply(
        _decoded(
            _frame(
                clk="clk-2",
                publish_time_ms=200,
                orders=[
                    _order(
                        status="EC",
                        average_price_matched=2.5,
                        size_matched=3,
                        size_remaining=7,
                    )
                ],
            )
        )
    )
    matched = state.state_for_bet_id("bet-1")
    assert matched is not None
    assert matched.accepted_average_price == Decimal("2.5")


def test_heartbeat_advances_cursor_without_creating_execution_truth() -> None:
    state = _initialized_state()
    heartbeat = _decoded(
        _frame(
            clk="clk-2",
            publish_time_ms=200,
            change_type="HEARTBEAT",
            include_market=False,
        )
    )

    result = state.apply(heartbeat)

    assert result.status is BetfairApplyStatus.HEARTBEAT
    assert result.changed == ()
    assert result.removed == ()
    assert result.cursor is not None and result.cursor.clk == "clk-2"
    assert ORDER_STREAM_FINAL_SETTLEMENT_AUTHORITY is False
