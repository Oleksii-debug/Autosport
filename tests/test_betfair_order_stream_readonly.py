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
    BetfairProviderStreamHealth,
)

SUBSCRIPTION_SHA256 = "a" * 64


def _order(
    *,
    bet_id: str = "bet-1",
    customer_order_ref: str | None = "attempt-ref-1",
    customer_strategy_ref: str | None = "strategy-1",
    status: str = "E",
    average_price_matched: float | None = 0,
    size_matched: float = 0,
    size_remaining: float = 10,
    price: float = 2.4,
    size: float = 10,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": bet_id,
        "p": price,
        "s": size,
        "side": "B",
        "status": status,
        "pt": "L",
        "ot": "L",
        "pd": 1_790_000_000_000,
        "sm": size_matched,
        "sr": size_remaining,
        "sl": 0,
        "sc": 0,
        "sv": 0,
    }
    if average_price_matched is not None:
        result["avp"] = average_price_matched
    if customer_order_ref is not None:
        result["rfo"] = customer_order_ref
    if customer_strategy_ref is not None:
        result["rfs"] = customer_strategy_ref
    return result


def _runner(
    *,
    selection_id: int = 42,
    orders: list[dict[str, object]] | None = None,
    full_image: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": selection_id,
        "hc": 0,
        "uo": [] if orders is None else orders,
    }
    if full_image:
        result["fullImage"] = True
    return result


def _frame(
    *,
    clk: str,
    publish_time_ms: int,
    change_type: str | None = None,
    initial_clk: str | None = None,
    runners: list[dict[str, object]] | None = None,
    market_id: str = "1.234",
    market_full_image: bool = False,
    include_market: bool = True,
    provider_status: int | None = None,
) -> dict[str, object]:
    frame: dict[str, object] = {"op": "ocm", "clk": clk, "pt": publish_time_ms}
    if change_type is not None:
        frame["ct"] = change_type
    if initial_clk is not None:
        frame["initialClk"] = initial_clk
    if provider_status is not None:
        frame["status"] = provider_status
    if include_market:
        market: dict[str, object] = {
            "id": market_id,
            "orc": [_runner(orders=[_order()], full_image=True)] if runners is None else runners,
        }
        if market_full_image:
            market["fullImage"] = True
        frame["oc"] = [market]
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
            )
        )
    )
    return state


def test_ocm_uses_shared_framing_and_builds_subscription_bound_cursor() -> None:
    frame = _decoded(
        _frame(
            clk="clk-1",
            initial_clk="initial-1",
            publish_time_ms=100,
            change_type="SUB_IMAGE",
        )
    )
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)
    result = state.apply(frame)
    assert result.status is BetfairApplyStatus.APPLIED
    assert result.provider_health is BetfairProviderStreamHealth.UP_TO_DATE
    assert result.frame_kind is BetfairFrameKind.SUB_IMAGE
    assert result.cursor is not None
    assert result.cursor.subscription_sha256 == SUBSCRIPTION_SHA256
    assert result.cursor.initial_clk == "initial-1"
    assert result.cursor.clk == "clk-1"
    assert state.state_for_bet_id("bet-1") is not None


def test_customer_order_ref_is_nonunique_correlation_not_provider_identity() -> None:
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)
    state.apply(
        _decoded(
            _frame(
                clk="clk-1",
                initial_clk="initial-1",
                publish_time_ms=100,
                change_type="SUB_IMAGE",
                runners=[
                    _runner(
                        full_image=True,
                        orders=[
                            _order(bet_id="bet-1", customer_order_ref="shared"),
                            _order(bet_id="bet-2", customer_order_ref="shared"),
                        ],
                    )
                ],
            )
        )
    )
    assert state.bet_ids_for_customer_order_ref("shared") == ("bet-1", "bet-2")
    assert state.bet_id_for_customer_order_ref("shared") is None


def test_removed_order_does_not_leave_stale_reference_alias() -> None:
    state = _initialized_state()
    result = state.apply(
        _decoded(
            _frame(
                clk="clk-2",
                publish_time_ms=200,
                runners=[_runner(full_image=True, orders=[])],
            )
        )
    )
    assert {item.bet_id for item in result.removed} == {"bet-1"}
    assert state.state_for_bet_id("bet-1") is None
    assert state.bet_ids_for_customer_order_ref("attempt-ref-1") == ()


def test_empty_customer_references_normalize_to_absence() -> None:
    raw = _order(customer_order_ref="", customer_strategy_ref="")
    frame = decode_order_change_message(
        _frame(clk="x", publish_time_ms=1, runners=[_runner(orders=[raw])])
    )
    order = frame.market_changes[0].runner_changes[0].unmatched_orders[0]
    assert order.customer_order_ref is None
    assert order.customer_strategy_ref is None


def test_line_market_position_at_or_below_one_is_preserved() -> None:
    frame = decode_order_change_message(
        _frame(
            clk="line",
            publish_time_ms=1,
            runners=[_runner(orders=[_order(price=0.5)])],
        )
    )
    order = frame.market_changes[0].runner_changes[0].unmatched_orders[0]
    assert order.price == Decimal("0.5")


def test_official_runner_fullimage_sub_image_needs_no_market_img() -> None:
    raw = _frame(
        clk="clk-1",
        initial_clk="initial-1",
        publish_time_ms=100,
        change_type="SUB_IMAGE",
        runners=[_runner(full_image=True, orders=[_order()])],
    )
    assert "img" not in raw["oc"][0]  # type: ignore[index]
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)
    result = state.apply(decode_order_change_message(raw))
    assert result.cursor is not None
    assert state.state_for_bet_id("bet-1") is not None


def test_mcm_img_key_does_not_mint_ocm_replacement_authority() -> None:
    state = _initialized_state()
    raw = _frame(
        clk="clk-2",
        publish_time_ms=200,
        runners=[_runner(orders=[_order(bet_id="bet-2", customer_order_ref="r2")])],
    )
    raw["oc"][0]["img"] = True  # type: ignore[index]
    state.apply(decode_order_change_message(raw))
    assert {item.identity.bet_id for item in state.snapshot()} == {"bet-1", "bet-2"}


def test_runner_fullimage_clears_only_addressed_runner() -> None:
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)
    state.apply(
        decode_order_change_message(
            _frame(
                clk="clk-1",
                initial_clk="initial-1",
                publish_time_ms=100,
                change_type="SUB_IMAGE",
                runners=[
                    _runner(selection_id=42, full_image=True, orders=[_order(bet_id="a", customer_order_ref="ra")]),
                    _runner(selection_id=43, full_image=True, orders=[_order(bet_id="b", customer_order_ref="rb")]),
                ],
            )
        )
    )
    result = state.apply(
        decode_order_change_message(
            _frame(
                clk="clk-2",
                publish_time_ms=200,
                runners=[_runner(selection_id=42, full_image=True, orders=[])],
            )
        )
    )
    assert {x.bet_id for x in result.removed} == {"a"}
    assert state.state_for_bet_id("a") is None
    assert state.state_for_bet_id("b") is not None


def test_market_fullimage_clears_market_and_indexes() -> None:
    state = _initialized_state()
    state.apply(
        decode_order_change_message(
            _frame(
                clk="clk-2",
                publish_time_ms=200,
                market_id="1.999",
                runners=[_runner(full_image=True, orders=[_order(bet_id="b", customer_order_ref="rb")])],
            )
        )
    )
    result = state.apply(
        decode_order_change_message(
            _frame(
                clk="clk-3",
                publish_time_ms=300,
                market_full_image=True,
                runners=[],
            )
        )
    )
    assert {x.bet_id for x in result.removed} == {"bet-1"}
    assert state.state_for_bet_id("b") is not None
    assert state.bet_ids_for_customer_order_ref("attempt-ref-1") == ()


def test_changed_uo_is_full_replacement_and_missing_core_field_rejects_atomically() -> None:
    state = _initialized_state()
    before_snapshot = state.snapshot()
    before_cursor = state.reconnect_cursor()
    partial = _order(status="EC", size_matched=10, size_remaining=0)
    del partial["sr"]
    with pytest.raises(ValueError, match="missing fields: sr"):
        state.apply(
            decode_order_change_message(
                _frame(clk="clk-2", publish_time_ms=200, runners=[_runner(orders=[partial])])
            )
        )
    assert state.snapshot() == before_snapshot
    assert state.reconnect_cursor() == before_cursor


def test_valid_full_order_replaces_absolute_fields_not_additive() -> None:
    state = _initialized_state()
    state.apply(
        decode_order_change_message(
            _frame(
                clk="clk-2",
                publish_time_ms=200,
                runners=[
                    _runner(
                        orders=[
                            _order(
                                status="EC",
                                average_price_matched=2.5,
                                size_matched=4,
                                size_remaining=6,
                            )
                        ]
                    )
                ],
            )
        )
    )
    order = state.state_for_bet_id("bet-1")
    assert order is not None
    assert order.size_matched == Decimal("4")
    assert order.size_remaining == Decimal("6")
    assert order.accepted_average_price == Decimal("2.5")


def test_provider_status_503_delta_cannot_advance_trusted_state_or_cursor() -> None:
    state = _initialized_state()
    before_snapshot, before_cursor = state.snapshot(), state.reconnect_cursor()
    with pytest.raises(ValueError, match="503"):
        state.apply(
            decode_order_change_message(
                _frame(
                    clk="clk-2",
                    publish_time_ms=200,
                    provider_status=503,
                    runners=[_runner(orders=[_order(bet_id="degraded")])],
                )
            )
        )
    assert state.snapshot() == before_snapshot
    assert state.reconnect_cursor() == before_cursor
    assert state.state_for_bet_id("degraded") is None


def test_provider_status_503_heartbeat_cannot_advance_trusted_cursor() -> None:
    state = _initialized_state()
    before_cursor = state.reconnect_cursor()
    with pytest.raises(ValueError, match="503"):
        state.apply(
            decode_order_change_message(
                _frame(
                    clk="clk-2",
                    publish_time_ms=200,
                    change_type="HEARTBEAT",
                    include_market=False,
                    provider_status=503,
                )
            )
        )
    assert state.reconnect_cursor() == before_cursor


def test_unknown_nonnull_provider_status_fails_decode() -> None:
    raw = _frame(clk="x", publish_time_ms=1, include_market=False)
    raw["status"] = 500
    with pytest.raises(ValueError, match="unsupported Betfair order-stream status"):
        decode_order_change_message(raw)


def test_rejected_second_order_cannot_leave_partial_state() -> None:
    state = _initialized_state()
    before_snapshot, before_cursor = state.snapshot(), state.reconnect_cursor()
    good = _order(bet_id="bet-2", customer_order_ref="r2")
    bad = _order(bet_id="bet-3", customer_order_ref="r3")
    del bad["sv"]
    with pytest.raises(ValueError):
        state.apply(
            decode_order_change_message(
                _frame(clk="clk-2", publish_time_ms=200, runners=[_runner(orders=[good, bad])])
            )
        )
    assert state.snapshot() == before_snapshot
    assert state.reconnect_cursor() == before_cursor
    assert state.state_for_bet_id("bet-2") is None


def test_same_clock_with_different_content_fails_closed() -> None:
    state = _initialized_state()
    state.apply(
        decode_order_change_message(
            _frame(clk="clk-2", publish_time_ms=200, runners=[_runner(orders=[_order(status="EC")])])
        )
    )
    with pytest.raises(ValueError, match="same Betfair order-stream clk"):
        state.apply(
            decode_order_change_message(
                _frame(
                    clk="clk-2",
                    publish_time_ms=200,
                    runners=[_runner(orders=[_order(status="EC", size_matched=1, size_remaining=9)])],
                )
            )
        )


def test_exact_replay_is_duplicate() -> None:
    state = _initialized_state()
    frame = decode_order_change_message(
        _frame(clk="clk-2", publish_time_ms=200, runners=[_runner(orders=[_order(status="EC")])])
    )
    assert state.apply(frame).status is BetfairApplyStatus.APPLIED
    assert state.apply(frame).status is BetfairApplyStatus.DUPLICATE


def test_delta_before_subscription_image_fails_closed() -> None:
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)
    with pytest.raises(ValueError, match="before SUB_IMAGE"):
        state.apply(decode_order_change_message(_frame(clk="clk-1", publish_time_ms=100)))


def test_resume_initial_clock_conflict_fails_closed() -> None:
    state = _initialized_state()
    with pytest.raises(ValueError, match="initialClk conflicts"):
        state.apply(
            decode_order_change_message(
                _frame(
                    clk="clk-2",
                    initial_clk="other-initial",
                    publish_time_ms=200,
                    change_type="RESUB_DELTA",
                )
            )
        )


def test_publish_time_cannot_move_backwards() -> None:
    state = _initialized_state()
    with pytest.raises(ValueError, match="publish time moved backwards"):
        state.apply(decode_order_change_message(_frame(clk="clk-2", publish_time_ms=99)))


def test_execution_complete_order_cannot_resurrect_to_executable() -> None:
    state = _initialized_state()
    state.apply(
        decode_order_change_message(
            _frame(
                clk="clk-2",
                publish_time_ms=200,
                runners=[_runner(orders=[_order(status="EC", size_matched=10, size_remaining=0)])],
            )
        )
    )
    with pytest.raises(ValueError, match="cannot become executable"):
        state.apply(
            decode_order_change_message(
                _frame(clk="clk-3", publish_time_ms=300, runners=[_runner(orders=[_order(status="E")])])
            )
        )


def test_average_price_raw_line_value_is_preserved_but_not_decimal_odds_authority() -> None:
    state = BetfairOrderStreamState(subscription_sha256=SUBSCRIPTION_SHA256)
    state.apply(
        decode_order_change_message(
            _frame(
                clk="clk-1",
                initial_clk="initial-1",
                publish_time_ms=100,
                change_type="SUB_IMAGE",
                runners=[
                    _runner(
                        full_image=True,
                        orders=[_order(price=0.5, average_price_matched=0.5, size_matched=1, size_remaining=9)],
                    )
                ],
            )
        )
    )
    order = state.state_for_bet_id("bet-1")
    assert order is not None
    assert order.average_price_matched == Decimal("0.5")
    assert order.accepted_average_price is None


def test_healthy_heartbeat_advances_cursor_without_execution_truth() -> None:
    state = _initialized_state()
    result = state.apply(
        decode_order_change_message(
            _frame(
                clk="clk-2",
                publish_time_ms=200,
                change_type="HEARTBEAT",
                include_market=False,
            )
        )
    )
    assert result.status is BetfairApplyStatus.HEARTBEAT
    assert result.changed == () and result.removed == ()
    assert result.cursor is not None and result.cursor.clk == "clk-2"
    assert ORDER_STREAM_FINAL_SETTLEMENT_AUTHORITY is False
