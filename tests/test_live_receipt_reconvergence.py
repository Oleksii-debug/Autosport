import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import autosport.market_mirror as market_mirror_module
import autosport.storage as storage_module
from autosport.domain import MarketEvent
from autosport.ingestion import IngestionEngine
from autosport.market_bus import MarketEventBus
from autosport.market_mirror import MarketMirror
from autosport.providers import CanonicalNormalizer, InMemoryProvider, ProviderQuote
from autosport.storage import SQLiteMarketStore


class LiveReceiptReconvergenceTests(unittest.TestCase):
    RECEIVE_TIME = "2026-10-04T03:00:02+00:00"

    @staticmethod
    def _quote(*, sequence: int = 1, odds: str = "2.00") -> ProviderQuote:
        return ProviderQuote(
            provider_event_id="event-1",
            provider_market_id="winner",
            provider_selection_id="selection-a",
            decimal_odds=Decimal(odds),
            observed_ts=f"2026-10-04T03:00:0{sequence}+00:00",
            sequence=sequence,
            source_ts=f"2026-10-04T02:59:5{sequence}+00:00",
        )

    @classmethod
    def _event(cls, *, sequence: int = 1) -> MarketEvent:
        event = CanonicalNormalizer().normalize(
            "provider-a",
            cls._quote(sequence=sequence),
        )
        return replace(event, ingest_ts=cls.RECEIVE_TIME)

    @classmethod
    def _ingest(cls, store: SQLiteMarketStore) -> MarketEvent:
        engine = IngestionEngine(
            MarketEventBus(store),
            clock=lambda: cls.RECEIVE_TIME,
        )
        stats = engine.poll_once(
            InMemoryProvider("provider-a", [cls._quote()]),
            max_items=10,
        )
        if stats.accepted != 1:
            raise AssertionError(f"expected one accepted event, got {stats.accepted}")
        return store.trusted_live_events()[0]

    def test_transaction_authority_survives_codec_json_and_identity_rebind(self) -> None:
        def poisoned_identity(_event):
            raise AssertionError("mutable identity descriptor must not be consulted")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            event = self._event()

            with (
                patch.object(MarketEvent, "quote_key", new=property(poisoned_identity)),
                patch.object(MarketEvent, "dedupe_key", new=property(poisoned_identity)),
                patch.object(
                    MarketEvent,
                    "from_dict",
                    side_effect=AssertionError("mutable from_dict must not be consulted"),
                ),
                patch.object(
                    MarketEvent,
                    "to_dict",
                    side_effect=AssertionError("mutable to_dict must not be consulted"),
                ),
                patch.object(
                    storage_module.json,
                    "loads",
                    side_effect=AssertionError("mutable json.loads must not be consulted"),
                ),
                patch.object(
                    storage_module.json,
                    "dumps",
                    side_effect=AssertionError("mutable json.dumps must not be consulted"),
                ),
            ):
                accepted = store._append_live_batch_accepted([event])
                trusted = store.trusted_live_events()
                current = store.trusted_live_current_by_source()
                has_receipt = store.has_trusted_live_receipt(event)
                store.close()

                reopened = SQLiteMarketStore(path)
                try:
                    trusted_after_restart = reopened.trusted_live_events()
                    current_after_restart = reopened.trusted_live_current_by_source()
                finally:
                    reopened.close()

            self.assertEqual(accepted, [event])
            self.assertEqual(trusted, [event])
            self.assertEqual(len(current), 1)
            self.assertTrue(has_receipt)
            self.assertEqual(trusted_after_restart, [event])
            self.assertEqual(len(current_after_restart), 1)

    def test_normalizer_cannot_reassign_provider_source_authority(self) -> None:
        class ForeignSourceNormalizer:
            def normalize(self, source_id, quote):
                canonical = CanonicalNormalizer().normalize(source_id, quote)
                return replace(canonical, source_id="provider-b")

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            engine = IngestionEngine(
                MarketEventBus(store),
                normalizer=ForeignSourceNormalizer(),
                clock=lambda: self.RECEIVE_TIME,
            )
            stats = engine.poll_once(
                InMemoryProvider("provider-a", [self._quote()]),
                max_items=10,
            )

            self.assertEqual(stats.accepted, 0)
            self.assertEqual(stats.rejected, 1)
            self.assertEqual(stats.quality_flags, ("INVALID_QUOTE",))
            self.assertEqual(store.events(), [])
            self.assertEqual(store.trusted_live_events(), [])
            store.close()

    def test_normalized_source_clock_matches_decision_freshness(self) -> None:
        class StaleSourceNormalizer:
            def normalize(self, source_id, quote):
                canonical = CanonicalNormalizer().normalize(source_id, quote)
                return replace(
                    canonical,
                    source_ts="2026-10-04T02:00:00+00:00",
                )

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            engine = IngestionEngine(
                MarketEventBus(store),
                normalizer=StaleSourceNormalizer(),
                clock=lambda: self.RECEIVE_TIME,
            )
            stats = engine.poll_once(
                InMemoryProvider("provider-a", [self._quote()]),
                max_items=10,
            )

            self.assertEqual(stats.accepted, 1)
            self.assertIn("STALE_SOURCE", stats.quality_flags)
            live = MarketMirror.from_live_store(store)
            decision = live.active_view(
                as_of=datetime.fromisoformat(self.RECEIVE_TIME),
                max_age=timedelta(seconds=120),
            )
            self.assertEqual(decision.events, ())
            store.close()

    def test_live_bus_snapshot_ignores_copy_and_codec_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            bus = MarketEventBus(store)
            delivered = []
            bus.subscribe(delivered.append)
            event = self._event()

            with (
                patch.object(
                    MarketEvent,
                    "__deepcopy__",
                    side_effect=AssertionError("mutable __deepcopy__ must not control live snapshots"),
                    create=True,
                ),
                patch.object(
                    MarketEvent,
                    "from_dict",
                    side_effect=AssertionError("mutable from_dict must not be consulted"),
                ),
                patch.object(
                    MarketEvent,
                    "to_dict",
                    side_effect=AssertionError("mutable to_dict must not be consulted"),
                ),
            ):
                accepted = MarketEventBus._publish_many_live_ingestion(bus, [event])

            self.assertEqual(accepted, 1)
            self.assertEqual(delivered, [event])
            self.assertTrue(store.has_trusted_live_receipt(event))
            store.close()

    def test_trusted_recovery_rejects_mirror_subclass_authority(self) -> None:
        class ForgingMirror(MarketMirror):
            def apply(self, event):
                raise AssertionError("subclass apply must not receive trusted authority")

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            self._ingest(store)

            with self.assertRaisesRegex(
                TypeError,
                "live store bootstrap requires an exact MarketMirror",
            ):
                ForgingMirror.from_live_store(store)
            with self.assertRaisesRegex(
                TypeError,
                "trusted live replay requires an exact MarketMirror",
            ):
                ForgingMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 10, 4, 3, 0, 3, tzinfo=timezone.utc),
                    max_age=timedelta(seconds=30),
                    require_live_receipt_authority=True,
                )
            store.close()

    def test_trusted_recovery_and_current_readers_ignore_runtime_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            self._ingest(store)

            with (
                patch.object(
                    SQLiteMarketStore,
                    "trusted_live_events",
                    side_effect=AssertionError("mutable trusted history method must not control current"),
                ),
                patch.object(
                    market_mirror_module,
                    "_require_market_store",
                    side_effect=AssertionError("mutable store guard helper must not be consulted"),
                ),
                patch.object(
                    market_mirror_module,
                    "_trusted_live_events",
                    side_effect=AssertionError("mutable trusted replay helper must not be consulted"),
                ),
                patch.object(
                    market_mirror_module,
                    "_trusted_live_current_by_source",
                    side_effect=AssertionError("mutable trusted current helper must not be consulted"),
                ),
            ):
                current = store.trusted_live_current_by_source()
                live = MarketMirror.from_live_store(store)
                replay = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 10, 4, 3, 0, 3, tzinfo=timezone.utc),
                    max_age=timedelta(seconds=30),
                    require_live_receipt_authority=True,
                )

            self.assertEqual(len(current), 1)
            self.assertEqual(len(live.snapshot()), 1)
            self.assertEqual(len(replay.events), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
