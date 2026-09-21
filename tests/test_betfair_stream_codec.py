from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autosport.betfair_stream_codec import (
    BETFAIR_STREAM_SOURCE_ID,
    BetfairApplyStatus,
    BetfairCrlfJsonDecoder,
    BetfairFrameKind,
    BetfairMarketStreamState,
    BetfairProviderStreamHealth,
    BetfairQuoteSide,
    decode_market_change_message,
)


def _image(*, clk: str = "clk-1", initial: str = "initial-1", pt: int = 1000) -> dict:
    return {
        "op": "mcm",
        "ct": "SUB_IMAGE",
        "initialClk": initial,
        "clk": clk,
        "pt": pt,
        "con": True,
        "mc": [
            {
                "id": "1.234",
                "img": True,
                "rc": [
                    {
                        "id": 101,
                        "hc": 0,
                        "ltp": 2.1,
                        "batb": [[0, 2.0, 10], [1, 1.99, 5]],
                        "batl": [[0, 2.12, 8]],
                    }
                ],
            }
        ],
    }


def test_crlf_decoder_handles_fragmented_multiple_frames() -> None:
    decoder = BetfairCrlfJsonDecoder()
    first = json.dumps({"op": "mcm", "pt": 1, "clk": "a", "mc": []}).encode()
    second = json.dumps({"op": "mcm", "pt": 2, "clk": "b", "mc": []}).encode()

    assert decoder.feed(first[:7]) == ()
    frames = decoder.feed(first[7:] + b"\r\n" + second + b"\r\n")

    assert [frame["clk"] for frame in frames] == ["a", "b"]
    decoder.finish()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"op":"mcm",\n"pt":1}\r\n',
        b'{"op":"mcm","pt":NaN}\r\n',
        b'{"op":"mcm","pt":1,"pt":2}\r\n',
        b'[]\r\n',
    ],
)
def test_crlf_decoder_fails_closed_on_ambiguous_or_noncanonical_json(payload: bytes) -> None:
    with pytest.raises(ValueError):
        BetfairCrlfJsonDecoder().feed(payload)


def test_crlf_decoder_rejects_truncated_frame() -> None:
    decoder = BetfairCrlfJsonDecoder()
    decoder.feed(b'{"op":"mcm"}')
    with pytest.raises(ValueError, match="truncated"):
        decoder.finish()


def test_sub_image_decodes_typed_identity_conflation_and_full_ladders() -> None:
    frame = decode_market_change_message(_image())
    assert frame.kind is BetfairFrameKind.SUB_IMAGE
    assert frame.initial_clk == "initial-1"
    assert frame.clk == "clk-1"
    assert frame.conflated is True

    runner = frame.market_changes[0].runner_changes[0]
    assert runner.selection_id == 101
    assert runner.handicap == Decimal("0")
    assert runner.last_traded_price == Decimal("2.1")
    assert [item.price for item in runner.available_to_back] == [
        Decimal("2.0"),
        Decimal("1.99"),
    ]

    state = BetfairMarketStreamState()
    result = state.apply(frame)
    identities = [item.identity for item in state.snapshot()]
    assert result.cursor.initial_clk == "initial-1"
    assert result.cursor.clk == "clk-1"
    assert all(identity.source_id == BETFAIR_STREAM_SOURCE_ID for identity in identities)
    assert {
        (identity.market_id, identity.selection_id, identity.handicap, identity.side)
        for identity in identities
    } >= {
        ("1.234", 101, Decimal("0"), BetfairQuoteSide.BACK),
        ("1.234", 101, Decimal("0"), BetfairQuoteSide.LAY),
        ("1.234", 101, Decimal("0"), BetfairQuoteSide.LAST_TRADED),
    }
    back_prices = {
        item.identity.price
        for item in state.snapshot()
        if item.identity.side is BetfairQuoteSide.BACK
    }
    assert back_prices == {Decimal("2.0"), Decimal("1.99")}


def test_delta_before_image_is_rejected() -> None:
    delta = decode_market_change_message(
        {"op": "mcm", "clk": "c2", "pt": 1001, "mc": [{"id": "1.234", "rc": []}]}
    )
    with pytest.raises(ValueError, match="before SUB_IMAGE"):
        BetfairMarketStreamState().apply(delta)


def test_delta_updates_ranked_ladder_and_heartbeat_advances_only_cursor() -> None:
    state = BetfairMarketStreamState()
    state.apply(decode_market_change_message(_image()))
    delta = decode_market_change_message(
        {
            "op": "mcm",
            "clk": "clk-2",
            "pt": 1001,
            "mc": [
                {
                    "id": "1.234",
                    "rc": [
                        {
                            "id": 101,
                            "hc": 0,
                            "batb": [[0, 2.02, 7], [1, 0, 0]],
                            "atl": [[2.2, 4]],
                        }
                    ],
                }
            ],
        }
    )

    result = state.apply(delta)
    back = [
        item for item in state.snapshot()
        if item.identity.side is BetfairQuoteSide.BACK
    ]
    assert [(item.price, item.size) for item in back] == [
        (Decimal("2.02"), Decimal("7"))
    ]
    assert {
        identity.price
        for identity in result.removed
        if identity.side is BetfairQuoteSide.BACK
    } == {Decimal("2.0"), Decimal("1.99")}

    before = state.snapshot()
    heartbeat = decode_market_change_message(
        {
            "op": "mcm",
            "ct": "HEARTBEAT",
            "initialClk": "initial-1",
            "clk": "clk-3",
            "pt": 1002,
            "mc": [],
        }
    )
    heartbeat_result = state.apply(heartbeat)
    assert heartbeat_result.status is BetfairApplyStatus.HEARTBEAT
    assert heartbeat_result.cursor.clk == "clk-3"
    assert state.snapshot() == before


def test_new_sub_image_replaces_prior_subscription_state() -> None:
    state = BetfairMarketStreamState()
    state.apply(decode_market_change_message(_image()))
    old = {item.identity for item in state.snapshot()}
    replacement = decode_market_change_message(
        {
            "op": "mcm",
            "ct": "SUB_IMAGE",
            "initialClk": "initial-2",
            "clk": "clk-10",
            "pt": 2000,
            "mc": [
                {
                    "id": "9.999",
                    "img": True,
                    "rc": [{"id": 202, "hc": 1.5, "ltp": 3.4}],
                }
            ],
        }
    )

    result = state.apply(replacement)

    assert set(result.removed) == old
    assert {item.identity.market_id for item in state.snapshot()} == {"9.999"}
    assert result.cursor.initial_clk == "initial-2"


def test_market_image_inside_delta_replaces_only_that_market() -> None:
    state = BetfairMarketStreamState()
    state.apply(decode_market_change_message(_image()))
    state.apply(
        decode_market_change_message(
            {
                "op": "mcm",
                "clk": "clk-2",
                "pt": 1001,
                "mc": [
                    {"id": "2.000", "img": True, "rc": [{"id": 303, "ltp": 4.0}]}
                ],
            }
        )
    )
    result = state.apply(
        decode_market_change_message(
            {
                "op": "mcm",
                "clk": "clk-3",
                "pt": 1002,
                "mc": [
                    {"id": "1.234", "img": True, "rc": [{"id": 101, "ltp": 2.5}]}
                ],
            }
        )
    )

    assert result.image_replaced_markets == ("1.234",)
    assert {item.identity.market_id for item in state.snapshot()} == {"1.234", "2.000"}
    first = [item for item in state.snapshot() if item.identity.market_id == "1.234"]
    assert len(first) == 1
    assert first[0].price == Decimal("2.5")


def test_resub_delta_requires_and_refreshes_initial_clock() -> None:
    state = BetfairMarketStreamState()
    state.apply(decode_market_change_message(_image()))

    missing = decode_market_change_message(
        {"op": "mcm", "ct": "RESUB_DELTA", "clk": "c2", "pt": 1001, "mc": []}
    )
    with pytest.raises(ValueError, match="requires initialClk"):
        state.apply(missing)

    refreshed = state.apply(
        decode_market_change_message(
            {
                "op": "mcm",
                "ct": "RESUB_DELTA",
                "initialClk": "initial-2",
                "clk": "c3",
                "pt": 1001,
                "mc": [],
            }
        )
    )
    assert refreshed.cursor.initial_clk == "initial-2"
    assert refreshed.cursor.clk == "c3"


def test_server_supplied_initial_clock_is_refreshed_on_later_messages() -> None:
    state = BetfairMarketStreamState()
    state.apply(decode_market_change_message(_image()))
    result = state.apply(
        decode_market_change_message(
            {
                "op": "mcm",
                "initialClk": "initial-3",
                "clk": "c4",
                "pt": 1002,
                "mc": [],
            }
        )
    )
    assert result.cursor.initial_clk == "initial-3"
    assert result.cursor.clk == "c4"


def test_same_clock_is_idempotent_only_for_identical_frame() -> None:
    state = BetfairMarketStreamState()
    raw = _image()
    state.apply(decode_market_change_message(raw))

    duplicate = state.apply(decode_market_change_message(raw))
    assert duplicate.status is BetfairApplyStatus.DUPLICATE

    changed = dict(raw)
    changed["pt"] = 1001
    with pytest.raises(ValueError, match="same Betfair clk"):
        state.apply(decode_market_change_message(changed))


def test_publish_time_regression_fails_closed() -> None:
    state = BetfairMarketStreamState()
    state.apply(decode_market_change_message(_image(pt=1000)))
    older = decode_market_change_message(
        {"op": "mcm", "clk": "clk-2", "pt": 999, "mc": []}
    )
    with pytest.raises(ValueError, match="moved backwards"):
        state.apply(older)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda raw: raw.update({"op": "ocm"}),
        lambda raw: raw["mc"][0]["rc"][0].update({"id": True}),
        lambda raw: raw["mc"][0]["rc"][0].update({"ltp": float("inf")}),
        lambda raw: raw["mc"][0].update({"rc": [{"id": 101}, {"id": 101}]}),
    ],
)
def test_provider_identity_and_numeric_ambiguity_fail_closed(mutator) -> None:
    raw = _image()
    mutator(raw)
    with pytest.raises((ValueError, TypeError)):
        decode_market_change_message(raw)


def test_resub_delta_preserves_opaque_cursor_without_sequence_math() -> None:
    state = BetfairMarketStreamState()
    state.apply(decode_market_change_message(_image()))
    result = state.apply(
        decode_market_change_message(
            {
                "op": "mcm",
                "ct": "RESUB_DELTA",
                "initialClk": "initial-1",
                "clk": "opaque-clk-token-zzz",
                "pt": 1005,
                "mc": [
                    {"id": "1.234", "rc": [{"id": 101, "hc": 0, "ltp": 2.3}]}
                ],
            }
        )
    )

    assert result.cursor.initial_clk == "initial-1"
    assert result.cursor.clk == "opaque-clk-token-zzz"
    ltp = [
        item for item in state.snapshot()
        if item.identity.side is BetfairQuoteSide.LAST_TRADED
    ]
    assert len(ltp) == 1
    assert ltp[0].price == Decimal("2.3")


def test_virtual_display_ladders_fail_closed_instead_of_silent_aliasing() -> None:
    raw = _image()
    runner = raw["mc"][0]["rc"][0]
    runner.pop("batb")
    runner["bdatb"] = [[0, 2.0, 10]]

    with pytest.raises(ValueError, match="virtual display ladder"):
        decode_market_change_message(raw)


@pytest.mark.parametrize("segment_type", ["SEG_START", "SEG", "SEG_END"])
def test_segmented_messages_require_reassembly_before_state_application(segment_type: str) -> None:
    raw = _image()
    raw["segmentType"] = segment_type

    with pytest.raises(ValueError, match="require reassembly"):
        decode_market_change_message(raw)


def test_stream_health_preserves_unreliable_heartbeat_without_refreshing_quotes() -> None:
    state = BetfairMarketStreamState()
    healthy = state.apply(decode_market_change_message(_image()))
    assert healthy.provider_health is BetfairProviderStreamHealth.UP_TO_DATE
    assert state.provider_health is BetfairProviderStreamHealth.UP_TO_DATE
    before = state.snapshot()

    unreliable = state.apply(
        decode_market_change_message(
            {
                "op": "mcm",
                "ct": "HEARTBEAT",
                "initialClk": "initial-1",
                "clk": "clk-health-503",
                "pt": 1001,
                "status": 503,
                "mc": [],
            }
        )
    )

    assert unreliable.status is BetfairApplyStatus.HEARTBEAT
    assert unreliable.provider_health is BetfairProviderStreamHealth.UNRELIABLE
    assert state.provider_health is BetfairProviderStreamHealth.UNRELIABLE
    assert state.last_unreliable_publish_time_ms == 1001
    assert state.snapshot() == before


def test_stream_health_is_preserved_on_unreliable_delta_and_recovery_does_not_erase_history() -> None:
    state = BetfairMarketStreamState()
    state.apply(decode_market_change_message(_image()))
    degraded = state.apply(
        decode_market_change_message(
            {
                "op": "mcm",
                "clk": "clk-health-delta",
                "pt": 1001,
                "status": 503,
                "mc": [{"id": "1.234", "rc": [{"id": 101, "hc": 0, "ltp": 2.3}]}],
            }
        )
    )
    assert degraded.status is BetfairApplyStatus.APPLIED
    assert degraded.provider_health is BetfairProviderStreamHealth.UNRELIABLE
    assert state.last_unreliable_publish_time_ms == 1001

    recovered = state.apply(
        decode_market_change_message(
            {
                "op": "mcm",
                "ct": "HEARTBEAT",
                "clk": "clk-health-recovered",
                "pt": 1002,
                "status": None,
                "mc": [],
            }
        )
    )
    assert recovered.provider_health is BetfairProviderStreamHealth.UP_TO_DATE
    assert state.provider_health is BetfairProviderStreamHealth.UP_TO_DATE
    assert state.last_unreliable_publish_time_ms == 1001


@pytest.mark.parametrize("status", [0, 500, "503", True, {"code": 503}])
def test_unknown_or_malformed_stream_health_fails_closed(status: object) -> None:
    raw = _image()
    raw["status"] = status
    with pytest.raises(ValueError, match="unsupported Betfair stream status"):
        decode_market_change_message(raw)


def test_same_clock_different_stream_health_is_not_a_duplicate() -> None:
    state = BetfairMarketStreamState()
    raw = _image()
    state.apply(decode_market_change_message(raw))

    unreliable = dict(raw)
    unreliable["status"] = 503
    with pytest.raises(ValueError, match="same Betfair clk"):
        state.apply(decode_market_change_message(unreliable))
