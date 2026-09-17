from decimal import Decimal
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
import tempfile

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate
from autosport.market_mirror_updates import BufferSubmit, MarketMirrorUpdateBuffer
from autosport.storage import SQLiteMarketStore


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
    assert [result.status for result in first.applied] == [
        MirrorUpdate.APPLIED,
        MirrorUpdate.APPLIED,
    ]
    assert [result.current_sequence for result in first.applied] == [1, 2]
    assert first.remaining == 1

    second = buffer.drain_provider("provider-a")
    assert [result.current_sequence for result in second.applied] == [3]
    assert second.remaining == 0
    assert mirror.get("provider-a", "event-1", "winner", "home").decimal_odds == Decimal(
        "2.20"
    )


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
    assert mirror.get("provider-b", "event-1", "winner", "home").decimal_odds == Decimal(
        "1.90"
    )


def test_submit_snapshots_input_value() -> None:
    mirror = MarketMirror()
    buffer = MarketMirrorUpdateBuffer(mirror, per_provider_capacity=1)
    event = _event("provider-a", 1, "2.00")

    assert buffer.submit(event).status is BufferSubmit.ACCEPTED
    drained = buffer.drain_provider("provider-a")

    assert len(drained.applied) == 1
    assert mirror.get("provider-a", "event-1", "winner", "home") == event


def test_persist_first_buffer_writes_durable_history_before_live_state() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteMarketStore(Path(directory) / "market.db")
        mirror = MarketMirror()
        buffer = MarketMirrorUpdateBuffer(mirror, store=store)
        event = _event("provider-a", 1, "2.00")
        try:
            buffer.submit(event)
            drained = buffer.drain_provider("provider-a")

            assert len(drained.applied) == 1
            assert store.events() == [event]
            assert mirror.get("provider-a", "event-1", "winner", "home") == event
        finally:
            store.close()


def test_persisted_same_sequence_conflict_is_terminal_and_quarantined() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteMarketStore(Path(directory) / "market.db")
        mirror = MarketMirror()
        buffer = MarketMirrorUpdateBuffer(mirror, store=store)
        first = _event("provider-a", 1, "2.00")
        conflict = _event("provider-a", 1, "9.99")
        try:
            buffer.submit(first)
            assert len(buffer.drain_provider("provider-a").applied) == 1

            buffer.submit(conflict)
            drained = buffer.drain_provider("provider-a")

            assert not drained.applied
            assert len(drained.quarantined) == 1
            assert drained.quarantined[0].error_type == "ValueError"
            assert drained.remaining == 0
            assert store.events() == [first]
            assert mirror.get("provider-a", "event-1", "winner", "home") == first
        finally:
            store.close()


def test_tampered_projection_value_error_retains_head_and_rolls_back_history() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteMarketStore(Path(directory) / "market.db")
        mirror = MarketMirror()
        buffer = MarketMirrorUpdateBuffer(mirror, store=store, per_provider_capacity=2)
        first = _event("provider-a", 1, "2.00")
        second = _event("provider-a", 2, "2.10")
        try:
            buffer.submit(first)
            assert len(buffer.drain_provider("provider-a").applied) == 1
            buffer.submit(second)

            store.connection.execute(
                "UPDATE current_quotes SET sequence=? WHERE source_id=? AND quote_key=?",
                (999, first.source_id, first.quote_key),
            )
            store.connection.commit()

            try:
                buffer.drain_provider("provider-a")
            except RuntimeError as exc:
                assert str(exc) == "durable market append failed before acknowledgement"
                assert isinstance(exc.__cause__, ValueError)
            else:
                raise AssertionError("tampered durable projection did not block acknowledgement")

            assert buffer.queued("provider-a") == 1
            assert store.events() == [first]
            assert mirror.get("provider-a", "event-1", "winner", "home") == first
        finally:
            store.close()


class _FailingProviderStore(SQLiteMarketStore):
    def append(self, event: MarketEvent) -> None:
        if event.source_id == "provider-a":
            raise sqlite3.OperationalError("forced provider-a append failure")
        super().append(event)


def test_operational_persistence_failure_retains_head_and_isolates_provider() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = _FailingProviderStore(Path(directory) / "market.db")
        mirror = MarketMirror()
        buffer = MarketMirrorUpdateBuffer(mirror, store=store, per_provider_capacity=2)
        first = _event("provider-a", 1, "2.00")
        other = _event("provider-b", 1, "1.90")
        buffer.submit(first)
        buffer.submit(other)
        try:
            try:
                buffer.drain_provider("provider-a")
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("forced durable append failure did not propagate")

            assert buffer.queued("provider-a") == 1
            assert mirror.get("provider-a", "event-1", "winner", "home") is None

            drained_b = buffer.drain_provider("provider-b")
            assert len(drained_b.applied) == 1
            assert buffer.queued("provider-b") == 0
            assert mirror.get("provider-b", "event-1", "winner", "home") == other
        finally:
            store.close()


class _BlockingMirror(MarketMirror):
    def __init__(self) -> None:
        super().__init__()
        self.first_entered = Event()
        self.second_entered = Event()
        self.release_first = Event()
        self._order_lock = Lock()
        self.apply_order: list[int] = []

    def apply(self, event: MarketEvent):
        with self._order_lock:
            self.apply_order.append(event.sequence)
        if event.sequence == 1:
            self.first_entered.set()
            if not self.release_first.wait(timeout=5):
                raise AssertionError("timed out waiting to release first provider update")
        elif event.sequence == 2:
            self.second_entered.set()
        return super().apply(event)


def test_same_provider_concurrent_drains_are_serialized_in_fifo_order() -> None:
    mirror = _BlockingMirror()
    buffer = MarketMirrorUpdateBuffer(mirror, per_provider_capacity=2)
    buffer.submit(_event("provider-a", 1, "2.00"))
    buffer.submit(_event("provider-a", 2, "2.10"))
    errors: list[BaseException] = []

    def drain_one() -> None:
        try:
            buffer.drain_provider("provider-a", limit=1)
        except BaseException as exc:
            errors.append(exc)

    first = Thread(target=drain_one)
    second = Thread(target=drain_one)
    first.start()
    assert mirror.first_entered.wait(timeout=5)

    second.start()
    assert not mirror.second_entered.wait(timeout=0.2)

    mirror.release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert mirror.apply_order == [1, 2]
    assert buffer.queued("provider-a") == 0
    assert mirror.get("provider-a", "event-1", "winner", "home").sequence == 2
