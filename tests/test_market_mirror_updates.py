from decimal import Decimal

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate
from autosport.market_mirror_updates import BufferSubmit, MarketMirrorUpdateBuffer


def _event(source: str, sequence: int, odds: str) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        decimal_odds=Decimal(odds),
        observed_ts="2026-09-17T02:00:00+00:00",
        ingest_ts="2026-09-17T02:00:00+00:00",
        source_id=source,
        sequence=sequence,
        status="open",
    )


def test_backpressure_is_bounded_and_provider_isolated() -> None:
    mirror = MarketMirror()
    buffer = MarketMirrorUpdateBuffer(mirror, per_provider_capacity=2)

    assert buffer.submit(_event("provider-a", 1, "2.00")).status is BufferSubmit.ACCEPTED
    assert buffer.submit(_event("provider-a", 2, "2.10")).status is BufferSubmit.ACCEPTED
    blocked = buffer.submit(_event("provider-a", 3, "2.20"))
    assert blocked.status is BufferSubmit.BACKPRESSURE
    assert blocked.queued == 2

    accepted_b = buffer.submit(_event("provider-b", 1, "1.90"))
    assert accepted_b.status is BufferSubmit.ACCEPTED
    assert buffer.queued("provider-a") == 2
    assert buffer.queued("provider-b") == 1


def test_provider_fifo_preserves_sequence_and_limit() -> None:
    mirror = MarketMirror()
    buffer = MarketMirrorUpdateBuffer(mirror, per_provider_capacity=4)
    buffer.submit(_event("provider-a", 1, "2.00"))
    buffer.submit(_event("provider-a", 2, "2.10"))
    buffer.submit(_event("provider-a", 3, "2.20"))

    first = buffer.drain_provider("provider-a", limit=2)
    assert [result.status for result in first.applied] == [MirrorUpdate.APPLIED, MirrorUpdate.APPLIED]
    assert [result.current_sequence for result in first.applied] == [1, 2]
    assert first.remaining == 1

    second = buffer.drain_provider("provider-a")
    assert [result.current_sequence for result in second.applied] == [3]
    assert second.remaining == 0
    assert mirror.get("provider-a", "event-1", "winner", "home").decimal_odds == Decimal("2.20")


def test_conflicting_provider_update_is_quarantined_without_stopping_other_updates() -> None:
    mirror = MarketMirror()
    buffer = MarketMirrorUpdateBuffer(mirror, per_provider_capacity=4)
    buffer.submit(_event("provider-a", 1, "2.00"))
    buffer.submit(_event("provider-a", 1, "9.99"))
    buffer.submit(_event("provider-a", 2, "2.10"))
    buffer.submit(_event("provider-b", 1, "1.90"))

    drained_a = buffer.drain_provider("provider-a")
    assert [result.current_sequence for result in drained_a.applied] == [1, 2]
    assert len(drained_a.quarantined) == 1
    assert drained_a.quarantined[0].error_type == "ValueError"
    assert drained_a.remaining == 0

    drained_b = buffer.drain_provider("provider-b")
    assert len(drained_b.applied) == 1
    assert not drained_b.quarantined
    assert mirror.get("provider-b", "event-1", "winner", "home").decimal_odds == Decimal("1.90")


def test_submit_snapshots_input_value() -> None:
    mirror = MarketMirror()
    buffer = MarketMirrorUpdateBuffer(mirror, per_provider_capacity=1)
    event = _event("provider-a", 1, "2.00")

    assert buffer.submit(event).status is BufferSubmit.ACCEPTED
    drained = buffer.drain_provider("provider-a")

    assert len(drained.applied) == 1
    assert mirror.get("provider-a", "event-1", "winner", "home") == event
