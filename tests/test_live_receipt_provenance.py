import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.domain import MarketEvent
from autosport.ingestion import IngestionEngine
from autosport.market_bus import MarketEventBus
from autosport.market_mirror import MarketMirror
from autosport.providers import CanonicalNormalizer, InMemoryProvider, ProviderQuote
from autosport.storage import SQLiteMarketStore


class LiveReceiptProvenanceTests(unittest.TestCase):
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
    def _direct_event(cls, *, sequence: int = 1, odds: str = "2.00"):
        event = CanonicalNormalizer().normalize(
            "provider-a",
            cls._quote(sequence=sequence, odds=odds),
        )
        return replace(event, ingest_ts=event.observed_ts)

    @classmethod
    def _ingest(cls, store: SQLiteMarketStore, *, sequence: int = 1) -> None:
        engine = IngestionEngine(
            MarketEventBus(store),
            clock=lambda: cls.RECEIVE_TIME,
        )
        stats = engine.poll_once(
            InMemoryProvider("provider-a", [cls._quote(sequence=sequence)]),
            max_items=10,
        )
        if stats.accepted != 1:
            raise AssertionError(f"expected one accepted live event, got {stats.accepted}")

    def test_direct_or_legacy_append_never_self_mints_live_receipt_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            event = self._direct_event()
            store = SQLiteMarketStore(path)
            self.assertTrue(store.append(event))
            self.assertFalse(store.has_trusted_live_receipt(event))
            self.assertEqual(store.trusted_live_events(), [])
            self.assertEqual(store.trusted_live_current_by_source(), {})
            store.close()

            reopened = SQLiteMarketStore(path)
            try:
                self.assertEqual(reopened.events(), [event])
                self.assertFalse(reopened.has_trusted_live_receipt(event))
                self.assertEqual(reopened.trusted_live_events(), [])
            finally:
                reopened.close()

    def test_generic_market_bus_remains_receipt_provenance_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            event = self._direct_event()
            store = SQLiteMarketStore(path)
            bus = MarketEventBus(store)

            self.assertEqual(bus.publish_many([event]), 1)
            self.assertEqual(store.events(), [event])
            self.assertFalse(store.has_trusted_live_receipt(event))
            self.assertEqual(store.trusted_live_events(), [])
            store.close()

    def test_public_batch_cannot_mint_receipt_from_mutable_pending_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            event = self._direct_event(sequence=1)
            batch = (event,)

            store._pending_live_receipt_batch = batch
            accepted = store.append_batch_accepted(batch)

            self.assertEqual(accepted, [event])
            self.assertFalse(store.has_trusted_live_receipt(event))
            self.assertEqual(store.trusted_live_events(), [])
            store.close()

    def test_live_receipt_authority_does_not_leak_into_reentrant_generic_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            live_event = self._direct_event(sequence=1)
            generic_event = self._direct_event(sequence=2, odds="2.20")

            def live_events():
                self.assertEqual(
                    store.append_batch_accepted([generic_event]),
                    [generic_event],
                )
                yield live_event

            accepted = store._append_live_batch_accepted(live_events())

            self.assertEqual(accepted, [live_event])
            self.assertFalse(store.has_trusted_live_receipt(generic_event))
            self.assertTrue(store.has_trusted_live_receipt(live_event))
            self.assertEqual(store.trusted_live_events(), [live_event])
            store.close()

    def test_normalizer_market_event_subclass_cannot_enter_live_receipt_path(self) -> None:
        class ForgingEvent(MarketEvent):
            @property
            def dedupe_key(self):
                raise AssertionError("subclass identity must not be consulted")

        class ForgingNormalizer:
            def normalize(self, source_id, quote):
                canonical = CanonicalNormalizer().normalize(source_id, quote)
                return ForgingEvent.from_dict(canonical.to_dict())

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            engine = IngestionEngine(
                MarketEventBus(store),
                normalizer=ForgingNormalizer(),
                clock=lambda: self.RECEIVE_TIME,
            )

            stats = engine.poll_once(
                InMemoryProvider("provider-a", [self._quote(sequence=1)]),
                max_items=10,
            )

            self.assertEqual(stats.accepted, 0)
            self.assertEqual(stats.rejected, 1)
            self.assertEqual(stats.quality_flags, ("INVALID_QUOTE",))
            self.assertEqual(store.events(), [])
            self.assertEqual(store.trusted_live_events(), [])
            store.close()

    def test_market_bus_subclass_remains_receipt_provenance_neutral(self) -> None:
        class ForgingBus(MarketEventBus):
            def _publish_many_live_ingestion(self, events):
                forged = tuple(
                    replace(event, ingest_ts="2000-01-01T00:00:00+00:00")
                    for event in events
                )
                return self.store._append_live_batch_accepted(forged)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            engine = IngestionEngine(
                ForgingBus(store),
                clock=lambda: self.RECEIVE_TIME,
            )

            stats = engine.poll_once(
                InMemoryProvider("provider-a", [self._quote(sequence=1)]),
                max_items=10,
            )

            self.assertEqual(stats.accepted, 1)
            persisted = store.events()
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0].ingest_ts, self.RECEIVE_TIME)
            self.assertEqual(store.trusted_live_events(), [])
            store.close()

    def test_private_live_receipt_seam_rejects_store_subclass(self) -> None:
        class StoreSubclass(SQLiteMarketStore):
            pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = StoreSubclass(path)
            event = self._direct_event(sequence=1)

            with self.assertRaisesRegex(
                TypeError,
                "live receipt authority requires an exact SQLiteMarketStore",
            ):
                SQLiteMarketStore._append_live_batch_accepted(store, [event])

            self.assertEqual(store.events(), [])
            self.assertEqual(store.trusted_live_events(), [])
            store.close()

    def test_live_ingestion_rejects_store_subclass_receipt_override(self) -> None:
        class ForgingStore(SQLiteMarketStore):
            def _append_live_batch_accepted(self, events):
                forged = tuple(
                    replace(event, ingest_ts="2000-01-01T00:00:00+00:00")
                    for event in events
                )
                return SQLiteMarketStore._append_live_batch_accepted(self, forged)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = ForgingStore(path)
            engine = IngestionEngine(
                MarketEventBus(store),
                clock=lambda: self.RECEIVE_TIME,
            )

            with self.assertRaisesRegex(
                TypeError,
                "live ingestion requires an exact SQLiteMarketStore",
            ):
                engine.poll_once(
                    InMemoryProvider("provider-a", [self._quote(sequence=1)]),
                    max_items=10,
                )

            self.assertEqual(store.events(), [])
            self.assertEqual(store.trusted_live_events(), [])
            store.close()

    def test_store_subclass_cannot_forge_live_bootstrap_or_replay_authority(self) -> None:
        class ForgingStore(SQLiteMarketStore):
            def trusted_live_events(self):
                return self.events()

            def trusted_live_current_by_source(self):
                return self.current_by_source()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = ForgingStore(path)
            event = self._direct_event(sequence=1)
            self.assertTrue(store.append(event))

            with self.assertRaisesRegex(
                TypeError,
                "live store must be an exact SQLiteMarketStore",
            ):
                MarketMirror.from_live_store(store)
            with self.assertRaisesRegex(
                TypeError,
                "trusted live replay requires an exact SQLiteMarketStore",
            ):
                MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 10, 4, 3, 0, 3, tzinfo=timezone.utc),
                    max_age=timedelta(seconds=30),
                    require_live_receipt_authority=True,
                )
            generic_replay = MarketMirror.replay_view_from_store(
                store,
                as_of=datetime(2026, 10, 4, 3, 0, 3, tzinfo=timezone.utc),
                max_age=timedelta(seconds=30),
            )

            self.assertEqual(generic_replay.events, (event,))
            store.close()

    def test_live_ingestion_persists_market_row_and_receipt_authority_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            self._ingest(store)

            persisted = store.events()
            self.assertEqual(len(persisted), 1)
            event = persisted[0]
            self.assertEqual(event.ingest_ts, self.RECEIVE_TIME)
            self.assertTrue(store.has_trusted_live_receipt(event))
            self.assertEqual(store.trusted_live_events(), [event])
            self.assertEqual(
                store.trusted_live_current_by_source()[(event.source_id, event.quote_key)],
                event,
            )
            receipt = store.connection.execute(
                """SELECT ingest_ts,authority
                   FROM market_event_live_receipts
                   WHERE dedupe_key=?""",
                (event.dedupe_key,),
            ).fetchone()
            self.assertEqual(
                receipt,
                (self.RECEIVE_TIME, "autosport.live_ingestion_receipt.v1"),
            )
            store.close()

    def test_live_duplicate_cannot_retroactively_upgrade_untrusted_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            original = self._direct_event()
            store = SQLiteMarketStore(path)
            self.assertTrue(store.append(original))

            engine = IngestionEngine(
                MarketEventBus(store),
                clock=lambda: self.RECEIVE_TIME,
            )
            stats = engine.poll_once(
                InMemoryProvider("provider-a", [self._quote(sequence=1)]),
                max_items=10,
            )

            self.assertEqual(stats.accepted, 0)
            self.assertEqual(store.events(), [original])
            self.assertFalse(store.has_trusted_live_receipt(original))
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM market_event_live_receipts"
                ).fetchone()[0],
                0,
            )
            store.close()

    def test_live_bootstrap_ignores_newer_untrusted_projection_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            self._ingest(store, sequence=1)
            trusted = store.trusted_live_events()[0]

            untrusted = self._direct_event(sequence=2, odds="2.20")
            self.assertTrue(store.append(untrusted))
            self.assertFalse(store.has_trusted_live_receipt(untrusted))

            generic = MarketMirror.from_store(store)
            live = MarketMirror.from_live_store(store)
            generic_event = generic.snapshot()[0]
            live_event = live.snapshot()[0]
            self.assertEqual(generic_event.sequence, 2)
            self.assertEqual(live_event.sequence, 1)
            self.assertEqual(live_event, trusted)
            store.close()

    def test_live_recovery_replay_excludes_untrusted_history_until_trusted_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            legacy = self._direct_event(sequence=1)
            self.assertTrue(store.append(legacy))
            self._ingest(store, sequence=2)

            before_live_receipt = datetime(
                2026, 10, 4, 3, 0, 1, 500000, tzinfo=timezone.utc
            )
            generic = MarketMirror.replay_view_from_store(
                store,
                as_of=before_live_receipt,
                max_age=timedelta(seconds=30),
            )
            trusted = MarketMirror.replay_view_from_store(
                store,
                as_of=before_live_receipt,
                max_age=timedelta(seconds=30),
                require_live_receipt_authority=True,
            )
            self.assertEqual(tuple(event.sequence for event in generic.events), (1,))
            self.assertEqual(trusted.events, ())

            after_live_receipt = datetime(
                2026, 10, 4, 3, 0, 3, tzinfo=timezone.utc
            )
            trusted_after = MarketMirror.replay_view_from_store(
                store,
                as_of=after_live_receipt,
                max_age=timedelta(seconds=30),
                require_live_receipt_authority=True,
            )
            self.assertEqual(
                tuple(event.sequence for event in trusted_after.events),
                (2,),
            )
            store.close()

    def test_receipt_persistence_failure_rolls_back_market_insert_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            with patch.object(
                store,
                "_insert_live_receipt_authority",
                side_effect=sqlite3.OperationalError("receipt write failed"),
            ):
                with self.assertRaisesRegex(sqlite3.OperationalError, "receipt write failed"):
                    self._ingest(store)

            self.assertEqual(store.events(), [])
            self.assertEqual(store.current_by_source(), {})
            self.assertEqual(store.trusted_live_events(), [])
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM market_event_live_receipts"
                ).fetchone()[0],
                0,
            )
            store.close()

    def test_pre_receipt_schema_reopens_without_retroactive_trust(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            event = self._direct_event()
            store = SQLiteMarketStore(path)
            self.assertTrue(store.append(event))
            store.connection.execute("DROP TABLE market_event_live_receipts")
            store.connection.commit()
            store.close()

            reopened = SQLiteMarketStore(path)
            try:
                self.assertEqual(reopened.events(), [event])
                self.assertEqual(reopened.trusted_live_events(), [])
                self.assertFalse(reopened.has_trusted_live_receipt(event))
                receipt_table = reopened.connection.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name='market_event_live_receipts'"""
                ).fetchone()
                self.assertEqual(receipt_table, ("market_event_live_receipts",))
            finally:
                reopened.close()

    def test_tampered_live_receipt_authority_fails_closed_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            self._ingest(store)
            event = store.events()[0]
            store.close()

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """UPDATE market_event_live_receipts
                       SET ingest_ts=?
                       WHERE dedupe_key=?""",
                    ("2026-10-04T03:00:09+00:00", event.dedupe_key),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "live receipt authority ingest_ts does not match market history",
            ):
                SQLiteMarketStore(path)

    def test_orphan_live_receipt_authority_fails_closed_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            store.close()

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """INSERT INTO market_event_live_receipts
                       (dedupe_key,ingest_ts,authority)
                       VALUES (?,?,?)""",
                    (
                        "missing-history",
                        self.RECEIVE_TIME,
                        "autosport.live_ingestion_receipt.v1",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "references missing market history",
            ):
                SQLiteMarketStore(path)


if __name__ == "__main__":
    unittest.main()
