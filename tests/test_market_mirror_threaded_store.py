from decimal import Decimal
from pathlib import Path
from threading import Thread
import tempfile

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_updates import BufferSubmit, MarketMirrorUpdateBuffer
from autosport.storage import SQLiteMarketStore


def _event(source: str, sequence: int, odds: str) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        decimal_odds=Decimal(odds),
        observed_ts=f"2026-09-17T02:00:0{sequence}+00:00",
        ingest_ts=f"2026-09-17T02:00:0{sequence}+00:00",
        source_id=source,
        sequence=sequence,
        status="open",
    )


def test_durable_multi_provider_threaded_drains_are_serialized_and_reopen() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "market.db"
        store = SQLiteMarketStore(path)
        mirror = MarketMirror()
        buffer = MarketMirrorUpdateBuffer(mirror, store=store, per_provider_capacity=4)
        events = {
            "provider-a": (_event("provider-a", 1, "2.00"), _event("provider-a", 2, "2.10")),
            "provider-b": (_event("provider-b", 1, "1.90"), _event("provider-b", 2, "1.95")),
        }
        for provider_events in events.values():
            for event in provider_events:
                assert buffer.submit(event).status is BufferSubmit.ACCEPTED

        errors: list[BaseException] = []
        results = {}

        def drain(source_id: str) -> None:
            try:
                results[source_id] = buffer.drain_provider(source_id)
            except BaseException as exc:
                errors.append(exc)

        threads = [Thread(target=drain, args=(source_id,)) for source_id in events]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert all(not thread.is_alive() for thread in threads)
        assert not errors
        assert [item.current_sequence for item in results["provider-a"].applied] == [1, 2]
        assert [item.current_sequence for item in results["provider-b"].applied] == [1, 2]
        assert buffer.queued() == 0

        current = store.current_by_source()
        for source_id, provider_events in events.items():
            latest = provider_events[-1]
            assert current[(source_id, latest.quote_key)] == latest
        assert len(store.events()) == 4
        durable_dedupe_keys = {event.dedupe_key for event in store.events()}
        expected_dedupe_keys = {
            event.dedupe_key
            for provider_events in events.values()
            for event in provider_events
        }
        assert durable_dedupe_keys == expected_dedupe_keys
        store.close()

        reopened = SQLiteMarketStore(path)
        try:
            assert {event.dedupe_key for event in reopened.events()} == expected_dedupe_keys
            reopened_current = reopened.current_by_source()
            for source_id, provider_events in events.items():
                latest = provider_events[-1]
                assert reopened_current[(source_id, latest.quote_key)] == latest
        finally:
            reopened.close()
