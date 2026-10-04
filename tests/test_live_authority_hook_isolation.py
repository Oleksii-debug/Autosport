import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent, MarketType
from autosport.ingestion import IngestionEngine
from autosport.market_bus import MarketEventBus
from autosport.market_mirror import MarketMirror
from autosport.storage import SQLiteMarketStore


class LiveAuthorityHookIsolationTests(unittest.TestCase):
    def _event(self, *, sequence: int = 1) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="winner",
            selection_id="selection-a",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-10-04T03:00:01+00:00",
            source_id="provider-a",
            sequence=sequence,
            market_type=MarketType.WINNER,
            ingest_ts="2026-10-04T03:00:02+00:00",
            metadata={"origin": "canonical"},
        )

    def test_public_store_authority_surfaces_reject_dependency_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            event = self._event()

            with self.assertRaises(TypeError):
                store.append_batch_accepted(
                    [event],
                    _capability_type=type(None),
                    _capability_authorizes=lambda *_args: True,
                )
            self.assertEqual(store.events(), [])

            self.assertEqual(store.append_batch_accepted([event]), [event])
            self.assertFalse(store.has_trusted_live_receipt(event))
            self.assertEqual(store.trusted_live_events(), [])

            with self.assertRaises(TypeError):
                store.has_trusted_live_receipt(
                    event,
                    _read=lambda _connection, _event: True,
                )
            with self.assertRaises(TypeError):
                store.trusted_live_events(
                    _read=lambda _connection: [event],
                )
            with self.assertRaises(TypeError):
                store.trusted_live_current_by_source(
                    _read=lambda _connection: [event],
                )

            self.assertFalse(store.has_trusted_live_receipt(event))
            self.assertEqual(store.trusted_live_events(), [])
            self.assertEqual(store.trusted_live_current_by_source(), {})
            store.close()

    def test_private_live_store_seam_rejects_dependency_override_but_canonical_path_works(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            event = self._event()

            with self.assertRaises(TypeError):
                store._append_live_batch_accepted(
                    [event],
                    _market_event_type=object,
                )
            self.assertEqual(store.events(), [])

            self.assertEqual(store._append_live_batch_accepted([event]), [event])
            self.assertTrue(store.has_trusted_live_receipt(event))

            with self.assertRaises(TypeError):
                store._rebuild_trusted_live_current_quotes(
                    _trusted_events=lambda _connection: [],
                )
            self.assertEqual(
                store.trusted_live_current_by_source(),
                {(event.source_id, event.quote_key): event},
            )
            store.close()

    def test_receipt_writer_requires_active_live_capability_and_rejects_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            event = self._event()
            try:
                self.assertTrue(store.append(event))
                self.assertFalse(store.has_trusted_live_receipt(event))

                with self.assertRaisesRegex(
                    PermissionError,
                    "internal to canonical live ingestion",
                ):
                    store._insert_live_receipt_authority(event)
                with self.assertRaises(TypeError):
                    store._insert_live_receipt_authority(
                        event,
                        _authority="forged-authority",
                    )

                self.assertFalse(store.has_trusted_live_receipt(event))
                self.assertEqual(store.trusted_live_current_by_source(), {})
            finally:
                store.close()

    def test_live_bus_seam_rejects_dependency_override_but_canonical_path_works(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            bus = MarketEventBus(store)
            event = self._event()

            with self.assertRaises(TypeError):
                bus._publish_many_live_ingestion(
                    [event],
                    _store_type=object,
                )
            self.assertEqual(store.events(), [])

            self.assertEqual(bus._publish_many_live_ingestion([event]), 1)
            self.assertTrue(store.has_trusted_live_receipt(event))
            store.close()

    def test_ingestion_poll_rejects_caller_authority_hooks_before_provider_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            engine = IngestionEngine(MarketEventBus(store))

            for name, value in (
                ("_stamp", lambda event, now: event),
                ("_publish", lambda bus, events: len(events)),
                ("_parse_timestamp", lambda value: value),
            ):
                with self.subTest(name=name):
                    with self.assertRaises(TypeError):
                        engine.poll_once(object(), **{name: value})

            self.assertEqual(store.events(), [])
            self.assertEqual(store.trusted_live_events(), [])
            store.close()

    def test_trusted_replay_and_bootstrap_reject_caller_dependency_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            event = self._event()
            self.assertTrue(store.append(event))

            replay_args = {
                "as_of": datetime(2026, 10, 4, 3, 0, 5, tzinfo=timezone.utc),
                "max_age": timedelta(seconds=30),
                "require_live_receipt_authority": True,
            }
            with self.assertRaises(TypeError):
                MarketMirror.replay_view_from_store(
                    store,
                    _trusted_reader=lambda _store: [event],
                    **replay_args,
                )
            with self.assertRaises(TypeError):
                MarketMirror.replay_view_from_store(
                    store,
                    _require_store=lambda candidate, **_kwargs: candidate,
                    **replay_args,
                )
            with self.assertRaises(TypeError):
                MarketMirror.from_live_store(
                    store,
                    _trusted_current=lambda _store: {
                        (event.source_id, event.quote_key): event
                    },
                )
            with self.assertRaises(TypeError):
                MarketMirror.from_live_store(
                    store,
                    _object_new=object.__new__,
                )

            replay = MarketMirror.replay_view_from_store(store, **replay_args)
            live = MarketMirror.from_live_store(store)
            self.assertEqual(replay.events, ())
            self.assertEqual(len(live), 0)
            store.close()


if __name__ == "__main__":
    unittest.main()
