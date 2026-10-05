from __future__ import annotations

import json
from decimal import Decimal

import pytest

import autosport.betfair_stream_codec as stream_codec

from autosport.betfair_stream_codec import (
    BETFAIR_STREAM_SOURCE_ID,
    BetfairApplyStatus,
    BetfairCrlfJsonDecoder,
    BetfairFrameKind,
    BetfairMarketChangeSegmentReassembler,
    BetfairMarketStreamState,
    BetfairProviderStreamHealth,
    BetfairQuoteSide,
    BetfairSegmentBudget,
    decode_market_change_message,
)


def _image(*, clk: str = "clk-1", initial: str = "initial-1", pt: int = 1000) -> dict:
    return {
        "op": "mcm",
        "ct": "SUB_IMAGE",
        "initialClk": initial,
        "clk": clk,
        "pt": pt,
        "mc": [
            {
                "id": "1.234",
                "img": True,
                "con": True,
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


def test_crlf_decoder_poison_after_mixed_valid_invalid_batch_prevents_frame_skip() -> None:
    decoder = BetfairCrlfJsonDecoder()
    good_a = b'{"op":"mcm","pt":1,"clk":"a","mc":[]}\r\n'
    bad_b = b'{"op":"mcm","pt":NaN,"clk":"bad","mc":[]}\r\n'
    good_c = b'{"op":"mcm","pt":3,"clk":"c","mc":[]}\r\n'

    with pytest.raises(ValueError, match="strict UTF-8 JSON"):
        decoder.feed(good_a + bad_b + good_c)

    with pytest.raises(ValueError, match="decoder failed"):
        decoder.feed(b'{"op":"mcm","pt":4,"clk":"d","mc":[]}\r\n')
    with pytest.raises(ValueError, match="decoder failed"):
        decoder.finish()


def test_market_level_conflation_is_authoritative_over_nonschema_top_level_con() -> None:
    raw = _image()
    raw["con"] = False
    raw["mc"][0]["con"] = True
    assert decode_market_change_message(raw).conflated is True

    raw = _image()
    raw["con"] = True
    raw["mc"][0]["con"] = False
    assert decode_market_change_message(raw).conflated is False


def test_market_level_conflation_is_aggregated_and_malformed_value_fails_closed() -> None:
    raw = _image()
    raw["mc"][0]["con"] = False
    raw["mc"].append({"id": "2.000", "img": True, "con": True, "rc": []})
    frame = decode_market_change_message(raw)
    assert frame.conflated is True
    assert [market.conflated for market in frame.market_changes] == [False, True]

    malformed = _image()
    malformed["mc"][0]["con"] = "true"
    with pytest.raises(ValueError, match="market.con"):
        decode_market_change_message(malformed)


@pytest.mark.parametrize("heartbeat_ms", [500, 5000, 5001, 30000])
def test_server_reported_inbound_heartbeat_bounds_accept_documented_range(
    heartbeat_ms: int,
) -> None:
    raw = _image()
    raw["heartbeatMs"] = heartbeat_ms
    raw["conflateMs"] = 180000

    frame = decode_market_change_message(raw)
    assert frame.heartbeat_ms == heartbeat_ms
    assert frame.conflate_ms == 180000

    result = BetfairMarketStreamState().apply(frame)
    assert result.heartbeat_ms == heartbeat_ms
    assert result.conflate_ms == 180000


@pytest.mark.parametrize("heartbeat_ms", [499, 30001, True, 500.0, "5000"])
def test_server_reported_inbound_heartbeat_invalid_values_fail_closed(
    heartbeat_ms: object,
) -> None:
    raw = _image()
    raw["heartbeatMs"] = heartbeat_ms
    with pytest.raises(ValueError, match="heartbeatMs"):
        decode_market_change_message(raw)


@pytest.mark.parametrize("conflate_ms", [-1, True, 1.5, "180000"])
def test_server_reported_conflate_ms_requires_nonnegative_exact_integer(
    conflate_ms: object,
) -> None:
    raw = _image()
    raw["conflateMs"] = conflate_ms
    with pytest.raises(ValueError, match="conflateMs"):
        decode_market_change_message(raw)


def test_absent_server_timing_metadata_remains_explicitly_unknown() -> None:
    frame = decode_market_change_message(_image())
    assert frame.conflate_ms is None
    assert frame.heartbeat_ms is None

    result = BetfairMarketStreamState().apply(frame)
    assert result.conflate_ms is None
    assert result.heartbeat_ms is None


def _segmented_market(market_id: str) -> dict:
    return {"id": market_id, "img": True, "con": False, "rc": []}


def test_segment_reassembler_matches_reference_atomic_merge_law() -> None:
    reassembler = BetfairMarketChangeSegmentReassembler(
        budget=BetfairSegmentBudget(max_segments=4, max_canonical_bytes=100_000)
    )
    start = {
        "op": "mcm",
        "segmentType": "SEG_START",
        "pt": 1,
        "mc": [_segmented_market("2.000")],
    }
    middle = {
        "op": "mcm",
        "segmentType": "SEG",
        "pt": 2,
        "mc": [_segmented_market("3.000")],
    }
    end = _image()
    end["segmentType"] = "SEG_END"

    assert reassembler.push(start) is None
    assert reassembler.push(middle) is None
    completed = reassembler.push(end)

    assert completed is not None
    assert "segmentType" not in completed
    assert completed["pt"] == end["pt"]
    assert [item["id"] for item in completed["mc"]] == ["2.000", "3.000", "1.234"]
    assert reassembler.pending_segments == 0
    assert reassembler.pending_canonical_bytes == 0

    frame = decode_market_change_message(completed)
    assert frame.kind is BetfairFrameKind.SUB_IMAGE
    assert [market.market_id for market in frame.market_changes] == [
        "2.000",
        "3.000",
        "1.234",
    ]


@pytest.mark.parametrize("segment_type", ["SEG", "SEG_END"])
def test_segment_reassembler_rejects_middle_or_end_without_start_and_poison_latches(
    segment_type: str,
) -> None:
    reassembler = BetfairMarketChangeSegmentReassembler(
        budget=BetfairSegmentBudget(max_segments=4, max_canonical_bytes=100_000)
    )
    raw = {"op": "mcm", "segmentType": segment_type, "mc": []}

    with pytest.raises(ValueError, match="without SEG_START"):
        reassembler.push(raw)
    assert reassembler.failed is True
    assert reassembler.pending_segments == 0
    with pytest.raises(ValueError, match="reassembler failed"):
        reassembler.push(_image())


def test_segment_reassembler_rejects_nested_start_and_interleaved_unsegmented_message() -> None:
    budget = BetfairSegmentBudget(max_segments=4, max_canonical_bytes=100_000)

    nested = BetfairMarketChangeSegmentReassembler(budget=budget)
    start = {"op": "mcm", "segmentType": "SEG_START", "mc": []}
    assert nested.push(start) is None
    with pytest.raises(ValueError, match="SEG_START arrived before prior"):
        nested.push(start)
    assert nested.failed is True

    interleaved = BetfairMarketChangeSegmentReassembler(budget=budget)
    assert interleaved.push(start) is None
    with pytest.raises(ValueError, match="non-segmented message"):
        interleaved.push(_image())
    assert interleaved.failed is True


def test_segment_reassembler_enforces_finite_segment_count_and_clears_pending_memory() -> None:
    reassembler = BetfairMarketChangeSegmentReassembler(
        budget=BetfairSegmentBudget(max_segments=2, max_canonical_bytes=100_000)
    )
    assert reassembler.push(
        {"op": "mcm", "segmentType": "SEG_START", "mc": [_segmented_market("2.000")]}
    ) is None
    assert reassembler.push(
        {"op": "mcm", "segmentType": "SEG", "mc": [_segmented_market("3.000")]}
    ) is None

    with pytest.raises(ValueError, match="max_segments"):
        reassembler.push(
            {"op": "mcm", "segmentType": "SEG_END", "mc": [_segmented_market("4.000")]}
        )

    assert reassembler.failed is True
    assert reassembler.pending_segments == 0
    assert reassembler.pending_canonical_bytes == 0


def test_segment_reassembler_enforces_finite_canonical_byte_budget() -> None:
    reassembler = BetfairMarketChangeSegmentReassembler(
        budget=BetfairSegmentBudget(max_segments=10, max_canonical_bytes=1)
    )
    with pytest.raises(ValueError, match="max_canonical_bytes"):
        reassembler.push(
            {"op": "mcm", "segmentType": "SEG_START", "mc": [_segmented_market("2.000")]}
        )
    assert reassembler.failed is True
    assert reassembler.pending_segments == 0
    assert reassembler.pending_canonical_bytes == 0


@pytest.mark.parametrize(
    ("max_segments", "max_canonical_bytes"),
    [(0, 100), (1, 0), (True, 100), (1, True)],
)
def test_segment_budget_requires_positive_exact_integers(
    max_segments: object, max_canonical_bytes: object
) -> None:
    with pytest.raises(ValueError):
        BetfairSegmentBudget(
            max_segments=max_segments,  # type: ignore[arg-type]
            max_canonical_bytes=max_canonical_bytes,  # type: ignore[arg-type]
        )

def test_change_message_request_id_is_preserved_through_typed_state_result() -> None:
    raw = _image()
    raw["id"] = 42
    frame = decode_market_change_message(raw)
    assert frame.request_id == 42

    result = BetfairMarketStreamState().apply(frame)
    assert result.request_id == 42


@pytest.mark.parametrize("request_id", [-(2**31), 0, 2**31 - 1])
def test_change_message_request_id_accepts_exact_signed_int32_boundaries(
    request_id: int,
) -> None:
    raw = _image()
    raw["id"] = request_id
    assert decode_market_change_message(raw).request_id == request_id


@pytest.mark.parametrize("request_id", [True, "42", 42.0, -(2**31) - 1, 2**31])
def test_change_message_request_id_rejects_non_int32_values(request_id: object) -> None:
    raw = _image()
    raw["id"] = request_id
    with pytest.raises(ValueError, match="id must be a signed int32 or null"):
        decode_market_change_message(raw)


def test_missing_change_message_request_id_remains_explicitly_unproven() -> None:
    frame = decode_market_change_message(_image())
    assert frame.request_id is None
    result = BetfairMarketStreamState().apply(frame)
    assert result.request_id is None



class _FindTrackingBuffer(bytearray):
    def __init__(self) -> None:
        super().__init__()
        self.find_starts: list[int] = []

    def find(
        self,
        sub: bytes | bytearray,
        start: int = 0,
        end: int | None = None,
    ) -> int:
        self.find_starts.append(start)
        if end is None:
            return super().find(sub, start)
        return super().find(sub, start, end)


def test_crlf_decoder_fragment_search_reuses_validated_prefix() -> None:
    decoder = BetfairCrlfJsonDecoder()
    tracking = _FindTrackingBuffer()
    decoder._buffer = tracking

    payload = b'{"op":"mcm","pt":1,"clk":"' + (b"x" * 4096)
    for value in payload:
        assert decoder.feed(bytes([value])) == ()

    assert tracking.find_starts[0] == 0
    assert tracking.find_starts[-1] >= len(payload) - 2
    assert tracking.find_starts[-1] > 4000


def test_crlf_decoder_accepts_exact_max_frame_when_crlf_is_split() -> None:
    frame = b'{"op":"mcm","pt":1,"clk":"a","mc":[]}'
    decoder = BetfairCrlfJsonDecoder(max_frame_bytes=len(frame))

    assert decoder.feed(frame + b"\r") == ()
    decoded = decoder.feed(b"\n")

    assert decoded == ({"op": "mcm", "pt": 1, "clk": "a", "mc": []},)
    decoder.finish()


def test_crlf_decoder_rejects_payload_beyond_max_before_delimiter_and_latches() -> None:
    decoder = BetfairCrlfJsonDecoder(max_frame_bytes=8)

    with pytest.raises(ValueError, match="maximum size"):
        decoder.feed(b"123456789")

    with pytest.raises(ValueError, match="decoder failed"):
        decoder.feed(b"\r\n")


def test_crlf_decoder_maps_json_recursion_error_to_failed_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoder = BetfairCrlfJsonDecoder()

    def _raise_recursion(*args: object, **kwargs: object) -> object:
        raise RecursionError("synthetic JSON nesting overflow")

    monkeypatch.setattr(stream_codec.json, "loads", _raise_recursion)
    with pytest.raises(ValueError, match="strict UTF-8 JSON"):
        decoder.feed(b'{"op":"mcm","pt":1,"clk":"a","mc":[]}\r\n')

    with pytest.raises(ValueError, match="decoder failed"):
        decoder.feed(b'{"op":"mcm","pt":2,"clk":"b","mc":[]}\r\n')
