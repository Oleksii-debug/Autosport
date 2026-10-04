from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
import tempfile

import autosport.storage as storage_module
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


def test_persisted_duplicate_uses_canonical_durable_local_clocks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = SQLiteMarketStore(Path(directory) / "market.db")
        original = MarketEvent(
            event_id="event-1",
            market_id="winner",
            selection_id="home",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-09-17T02:00:00+00:00",
            ingest_ts="2026-09-17T02:00:00+00:00",
            source_id="provider-a",
            sequence=1,
            status="open",
        )
        retry = MarketEvent(
            event_id=original.event_id,
            market_id=original.market_id,
            selection_id=original.selection_id,
            decimal_odds=original.decimal_odds,
            observed_ts="2026-09-17T02:00:02+00:00",
            ingest_ts="2026-09-17T02:00:02+00:00",
            source_id=original.source_id,
            sequence=original.sequence,
            status=original.status,
        )
        try:
            assert original.dedupe_key == retry.dedupe_key
            assert store.append(original)

            mirror = MarketMirror()
            buffer = MarketMirrorUpdateBuffer(mirror, store=store)
            buffer.submit(retry)
            drained = buffer.drain_provider("provider-a")

            assert len(drained.applied) == 1
            assert drained.applied[0].status is MirrorUpdate.APPLIED
            live = mirror.get("provider-a", "event-1", "winner", "home")
            assert live == original
            assert live != retry
        finally:
            store.close()


def test_persisted_generation_zero_duplicate_stays_noncausal() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "market.db"
        legacy = _event("provider-a", 2, "2.20")
        payload = storage_module._canonical_payload(legacy)

        raw = sqlite3.connect(path)
        try:
            raw.execute(
                """CREATE TABLE market_events (
                    dedupe_key TEXT PRIMARY KEY,
                    quote_key TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    selection_id TEXT NOT NULL,
                    decimal_odds TEXT NOT NULL,
                    observed_ts TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )"""
            )
            raw.execute(
                """CREATE TABLE current_quotes (
                    source_id TEXT NOT NULL,
                    quote_key TEXT NOT NULL,
                    observed_ts TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (source_id, quote_key)
                )"""
            )
            raw.execute(
                """INSERT INTO market_events
                   (dedupe_key,quote_key,event_id,market_id,selection_id,
                    decimal_odds,observed_ts,source_id,sequence,payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    legacy.dedupe_key,
                    legacy.quote_key,
                    legacy.event_id,
                    legacy.market_id,
                    legacy.selection_id,
                    str(legacy.decimal_odds),
                    legacy.observed_ts,
                    legacy.source_id,
                    legacy.sequence,
                    payload,
                ),
            )
            raw.execute(
                """INSERT INTO current_quotes
                   (source_id,quote_key,observed_ts,sequence,payload_json)
                   VALUES (?,?,?,?,?)""",
                (
                    legacy.source_id,
                    legacy.quote_key,
                    legacy.observed_ts,
                    legacy.sequence,
                    payload,
                ),
            )
            raw.commit()
        finally:
            raw.close()

        store = SQLiteMarketStore(path)
        try:
            assert store.connection.execute(
                """SELECT append_generation
                   FROM market_event_commit_order
                   WHERE dedupe_key=?""",
                (legacy.dedupe_key,),
            ).fetchone() == (0,)

            mirror = MarketMirror()
            buffer = MarketMirrorUpdateBuffer(mirror, store=store)
            buffer.submit(legacy)
            drained = buffer.drain_provider("provider-a")

            assert len(drained.applied) == 1
            assert mirror.get("provider-a", "event-1", "winner", "home") == legacy
            assert mirror.active_snapshot(
                as_of=datetime(2026, 9, 17, 2, 0, 1, tzinfo=timezone.utc),
                max_age=timedelta(minutes=2),
            ) == ()
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
