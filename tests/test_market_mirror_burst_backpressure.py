from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import BoundedMirrorInvalidationBuffer


def _event(
    *,
    selection: str = "selection-1",
    sequence: int,
    odds: Decimal,
) -> MarketEvent:
    timestamp = (
        datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
        + timedelta(milliseconds=sequence)
    ).isoformat()
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id=selection,
        decimal_odds=odds,
        observed_ts=timestamp,
        source_id="provider-a",
        sequence=sequence,
        status="open",
        source_ts=timestamp,
        ingest_ts=timestamp,
    )


def test_concurrent_same_quote_burst_converges_to_highest_sequence_and_one_dirty_key():
    mirror = MarketMirror()
    runtime = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=8)
    events = tuple(
        _event(
            sequence=sequence,
            odds=Decimal("2.000") + Decimal(sequence) / Decimal("1000"),
        )
        for sequence in range(1, 65)
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(runtime.accept_persisted, reversed(events)))

    current = mirror.get("provider-a", "event-1", "market-1", "selection-1")
    assert current is not None
    assert current.sequence == 64
    assert current.decimal_odds == Decimal("2.064")
    assert runtime.pending_count == 1
    assert runtime.full_refresh_required is False

    batch = runtime.drain(max_items=8)
    assert batch.changed_keys == (("provider-a", "event-1|market-1|selection-1"),)
    assert batch.full_refresh_required is False
    assert batch.has_more is False


def test_concurrent_distinct_key_burst_saturates_to_one_truthful_full_refresh_fence():
    mirror = MarketMirror()
    runtime = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=8)
    events = tuple(
        _event(
            selection=f"selection-{index:03d}",
            sequence=1,
            odds=Decimal("2.00") + Decimal(index) / Decimal("100"),
        )
        for index in range(64)
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(runtime.accept_persisted, events))

    snapshot = mirror.snapshot()
    assert len(snapshot) == 64
    assert {event.selection_id for event in snapshot} == {
        f"selection-{index:03d}" for index in range(64)
    }
    assert runtime.full_refresh_required is True
    assert runtime.pending_count == 0

    overflow = runtime.drain(max_items=4)
    assert overflow.full_refresh_required is True
    assert overflow.changed_keys == ()
    assert overflow.has_more is False
    assert runtime.full_refresh_required is False

    recovered = _event(
        selection="selection-after-refresh",
        sequence=1,
        odds=Decimal("3.00"),
    )
    runtime.accept_persisted(recovered)
    next_batch = runtime.drain(max_items=4)
    assert next_batch.full_refresh_required is False
    assert next_batch.changed_keys == (
        ("provider-a", "event-1|market-1|selection-after-refresh"),
    )
    assert next_batch.has_more is False
