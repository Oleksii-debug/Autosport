from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
)


_START = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)


def _event(
    selection: str,
    sequence: int,
    *,
    odds: str | None = None,
    source_id: str = "provider-a",
    event_id: str = "event-1",
    market_id: str = "market-1",
    status: str = "open",
    score_state: str | None = None,
) -> MarketEvent:
    timestamp = (_START + timedelta(seconds=sequence)).isoformat()
    return MarketEvent(
        event_id=event_id,
        market_id=market_id,
        selection_id=selection,
        decimal_odds=Decimal(odds or f"2.{sequence:02d}"),
        observed_ts=timestamp,
        source_id=source_id,
        sequence=sequence,
        status=status,
        source_ts=timestamp,
        ingest_ts=timestamp,
        score_state=score_state,
    )


def _key(selection: str) -> tuple[str, str]:
    return ("provider-a", f"event-1|market-1|{selection}")


def test_update_for_already_drained_quote_is_requeued_behind_existing_burst() -> None:
    mirror = MarketMirror()
    buffer = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=8)
    for selection in ("a", "b", "c"):
        buffer.accept_persisted(_event(selection, 1))

    first = buffer.drain(max_items=1)
    assert first.changed_keys == (_key("a"),)
    assert first.has_more is True

    # A newer A quote arrives after A's old invalidation has already been drained,
    # while B/C remain backpressured. It must become new downstream work rather
    # than being lost merely because the same quote appeared earlier in the burst.
    buffer.accept_persisted(_event("a", 2, odds="2.40"))

    middle = buffer.drain(max_items=2)
    assert middle.changed_keys == (_key("b"), _key("c"))
    assert middle.has_more is True

    last = buffer.drain(max_items=2)
    assert last.changed_keys == (_key("a"),)
    assert last.has_more is False
    assert buffer.pending_count == 0
    latest = mirror.get("provider-a", "event-1", "market-1", "a")
    assert latest is not None
    assert latest.sequence == 2
    assert latest.decimal_odds == Decimal("2.40")


def test_repeated_updates_to_still_pending_quote_coalesce_to_one_latest_work_item() -> None:
    mirror = MarketMirror()
    buffer = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=8)
    for selection in ("a", "b", "c"):
        buffer.accept_persisted(_event(selection, 1))

    assert buffer.drain(max_items=1).changed_keys == (_key("a"),)

    # B is still dirty. Multiple newer B updates must update canonical mirror truth
    # without multiplying the bounded invalidation queue or concealing the latest B.
    buffer.accept_persisted(_event("b", 2, odds="2.20"))
    buffer.accept_persisted(_event("b", 3, odds="2.30"))
    assert buffer.pending_count == 2

    second = buffer.drain(max_items=1)
    assert second.changed_keys == (_key("b"),)
    assert second.has_more is True
    latest = mirror.get("provider-a", "event-1", "market-1", "b")
    assert latest is not None
    assert latest.sequence == 3
    assert latest.decimal_odds == Decimal("2.30")

    third = buffer.drain(max_items=1)
    assert third.changed_keys == (_key("c"),)
    assert third.has_more is False


def test_same_quote_burst_stays_bounded_without_false_full_refresh() -> None:
    mirror = MarketMirror()
    buffer = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=1)

    for sequence in range(1, 41):
        buffer.accept_persisted(_event("a", sequence, odds=f"2.{sequence:02d}"))

    assert buffer.pending_count == 1
    assert buffer.full_refresh_required is False
    batch = buffer.drain(max_items=1)
    assert batch.changed_keys == (_key("a"),)
    assert batch.full_refresh_required is False
    assert batch.has_more is False
    latest = mirror.get("provider-a", "event-1", "market-1", "a")
    assert latest is not None
    assert latest.sequence == 40
    assert latest.decimal_odds == Decimal("2.40")


def test_partial_drain_then_distinct_burst_overflow_publishes_only_full_refresh() -> None:
    mirror = MarketMirror()
    buffer = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=2)
    buffer.accept_persisted(_event("a", 1))
    buffer.accept_persisted(_event("b", 1))

    first = buffer.drain(max_items=1)
    assert first.changed_keys == (_key("a"),)
    assert first.has_more is True

    buffer.accept_persisted(_event("c", 1))
    assert buffer.pending_count == 2
    buffer.accept_persisted(_event("d", 1))
    assert buffer.full_refresh_required is True
    assert buffer.pending_count == 0

    # Updates continue while the full-refresh fence is outstanding. They are applied
    # to canonical mirror state and are intentionally represented by that one fence.
    buffer.accept_persisted(_event("a", 2, odds="2.80"))
    overflow = buffer.drain(max_items=1)
    assert overflow.full_refresh_required is True
    assert overflow.changed_keys == ()
    assert overflow.has_more is False

    snapshot = {event.selection_id: event for event in mirror.snapshot()}
    assert set(snapshot) == {"a", "b", "c", "d"}
    assert snapshot["a"].sequence == 2
    assert snapshot["a"].decimal_odds == Decimal("2.80")

    # The full-refresh fence is one-shot; bounded incremental tracking resumes after
    # it is consumed rather than leaving the runtime permanently backpressured.
    buffer.accept_persisted(_event("e", 1))
    recovered = buffer.drain(max_items=1)
    assert recovered.full_refresh_required is False
    assert recovered.changed_keys == (_key("e"),)
    assert recovered.has_more is False


def test_full_refresh_rebuilds_focused_keys_from_latest_burst_truth() -> None:
    mirror = MarketMirror()
    buffer = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=1)
    dependencies = FocusedMirrorDependencyIndex(mirror)
    dependencies.register("input-a", selection_ids="a")
    dependencies.register("input-b", selection_ids="b")
    dependencies.register("unrelated", event_ids="event-2")

    buffer.accept_persisted(_event("a", 1, odds="2.00"))
    buffer.accept_persisted(_event("b", 1, odds="3.00"))
    assert buffer.full_refresh_required is True

    # This newer A arrives after overflow. affected_inputs(full-refresh) must rebuild
    # selector keys from mirror truth that already includes this update.
    buffer.accept_persisted(_event("a", 2, odds="2.50"))
    batch = buffer.drain(max_items=1)
    assert batch.full_refresh_required is True
    assert batch.changed_keys == ()
    assert dependencies.affected_inputs(batch) == ("input-a", "input-b", "unrelated")

    as_of = _START + timedelta(minutes=1)
    a_view = dependencies.incremental_decision_view(
        "input-a",
        as_of=as_of,
        max_age=timedelta(minutes=2),
    )
    b_view = dependencies.incremental_decision_view(
        "input-b",
        as_of=as_of,
        max_age=timedelta(minutes=2),
    )
    unrelated_view = dependencies.incremental_decision_view(
        "unrelated",
        as_of=as_of,
        max_age=timedelta(minutes=2),
    )

    assert len(a_view.events) == 1
    assert a_view.events[0].selection_id == "a"
    assert a_view.events[0].sequence == 2
    assert a_view.events[0].decimal_odds == Decimal("2.50")
    assert len(b_view.events) == 1
    assert b_view.events[0].selection_id == "b"
    assert unrelated_view.events == ()


def test_status_barrier_inside_same_quote_burst_forces_full_refresh() -> None:
    mirror = MarketMirror()
    buffer = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=8)

    buffer.accept_persisted(_event("barrier", 1, status="open"))
    assert buffer.pending_count == 1
    assert buffer.full_refresh_required is False

    # A suspension cannot be collapsed into the ordinary same-key dirty marker.
    # Even if a newer OPEN arrives before downstream drain, the outstanding fence
    # must force the live loop to rebuild cached decision state across the barrier.
    buffer.accept_persisted(_event("barrier", 2, status="suspended"))
    assert buffer.full_refresh_required is True
    assert buffer.pending_count == 0
    buffer.accept_persisted(_event("barrier", 3, status="open"))

    latest = mirror.get("provider-a", "event-1", "market-1", "barrier")
    assert latest is not None
    assert latest.sequence == 3
    assert latest.status == "open"

    batch = buffer.drain(max_items=8)
    assert batch.full_refresh_required is True
    assert batch.changed_keys == ()
    assert batch.has_more is False
    assert buffer.full_refresh_required is False


def test_material_score_transition_is_not_price_coalesced() -> None:
    mirror = MarketMirror()
    buffer = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=8)

    buffer.accept_persisted(_event("score", 1, score_state="0-0"))
    buffer.accept_persisted(_event("score", 2, odds="2.20", score_state="1-0"))

    assert buffer.full_refresh_required is True
    assert buffer.pending_count == 0
    batch = buffer.drain(max_items=8)
    assert batch.full_refresh_required is True
    assert batch.changed_keys == ()


def test_concurrent_same_quote_burst_converges_to_highest_sequence_and_one_dirty_key() -> None:
    mirror = MarketMirror()
    buffer = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=8)
    events = tuple(
        _event(
            "concurrent",
            sequence,
            odds=f"2.{sequence:02d}",
        )
        for sequence in range(1, 65)
    )

    # Submit newest-first to maximize stale-arrival pressure while eight callers race.
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(buffer.accept_persisted, reversed(events)))

    latest = mirror.get("provider-a", "event-1", "market-1", "concurrent")
    assert latest is not None
    assert latest.sequence == 64
    assert latest.decimal_odds == Decimal("2.64")
    assert buffer.pending_count == 1
    assert buffer.full_refresh_required is False

    batch = buffer.drain(max_items=8)
    assert batch.changed_keys == (_key("concurrent"),)
    assert batch.full_refresh_required is False
    assert batch.has_more is False


def test_concurrent_distinct_quote_burst_degrades_to_full_refresh_without_truth_loss() -> None:
    mirror = MarketMirror()
    buffer = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=8)
    events = tuple(
        _event(
            f"concurrent-{index:03d}",
            1,
            odds=f"{2 + index // 100}.{index % 100:02d}",
        )
        for index in range(64)
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(buffer.accept_persisted, events))

    snapshot = mirror.snapshot()
    assert len(snapshot) == 64
    assert {event.selection_id for event in snapshot} == {
        f"concurrent-{index:03d}" for index in range(64)
    }
    assert buffer.full_refresh_required is True
    assert buffer.pending_count == 0

    overflow = buffer.drain(max_items=4)
    assert overflow.full_refresh_required is True
    assert overflow.changed_keys == ()
    assert overflow.has_more is False
    assert buffer.full_refresh_required is False

    buffer.accept_persisted(_event("after-refresh", 1, odds="3.00"))
    recovered = buffer.drain(max_items=4)
    assert recovered.full_refresh_required is False
    assert recovered.changed_keys == (_key("after-refresh"),)
    assert recovered.has_more is False
