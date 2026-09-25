from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate
from autosport.storage import SQLiteMarketStore


class MarketMirrorPersistenceTests(unittest.TestCase):
    @staticmethod
    def event(
        *,
        source: str = "provider-a",
        sequence: int = 1,
        odds: str = "2.00",
        observed_ts: str = "2026-09-17T01:00:00+00:00",
    ) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            ingest_ts=observed_ts,
            source_id=source,
            sequence=sequence,
        )

    def test_persist_and_apply_survives_reopen_with_provider_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            mirror = MarketMirror()
            try:
                first = mirror.persist_and_apply(
                    store,
                    self.event(source="provider-a", odds="2.00"),
                )
                second = mirror.persist_and_apply(
                    store,
                    self.event(source="provider-b", odds="1.90"),
                )
                self.assertEqual(first.status, MirrorUpdate.APPLIED)
                self.assertEqual(second.status, MirrorUpdate.APPLIED)
                self.assertEqual(len(mirror), 2)
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                restored = MarketMirror.from_store(reopened)
                self.assertEqual(len(restored), 2)
                self.assertEqual(
                    restored.get(
                        "provider-a", "event-1", "market-1", "selection-1"
                    ).decimal_odds,
                    Decimal("2.00"),
                )
                self.assertEqual(
                    restored.get(
                        "provider-b", "event-1", "market-1", "selection-1"
                    ).decimal_odds,
                    Decimal("1.90"),
                )
            finally:
                reopened.close()

    def test_persistence_failure_cannot_mutate_live_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            mirror = MarketMirror()
            store.close()

            with self.assertRaises(sqlite3.ProgrammingError):
                mirror.persist_and_apply(store, self.event())

            self.assertEqual(mirror.snapshot(), ())

    def test_naive_ingest_timestamp_fails_before_durable_or_live_mutation(self) -> None:
        event = self.event(observed_ts="2026-09-17T01:00:00+00:00")
        naive_ingest = MarketEvent(
            event_id=event.event_id,
            market_id=event.market_id,
            selection_id=event.selection_id,
            decimal_odds=event.decimal_odds,
            observed_ts=event.observed_ts,
            ingest_ts="2026-09-17T01:00:01",
            source_id=event.source_id,
            sequence=event.sequence,
        )

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            mirror = MarketMirror()
            try:
                with self.assertRaisesRegex(ValueError, "ingest_ts must be timezone-aware"):
                    mirror.persist_and_apply(store, naive_ingest)
                self.assertEqual(store.events(), [])
                self.assertEqual(store.current(), {})
                self.assertEqual(mirror.snapshot(), ())
            finally:
                store.close()

    def test_duplicate_is_durable_idempotent_and_stale_history_does_not_regress_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            mirror = MarketMirror()
            try:
                newest = self.event(
                    sequence=2,
                    odds="2.20",
                    observed_ts="2026-09-17T01:00:00+00:00",
                )
                self.assertEqual(
                    mirror.persist_and_apply(store, newest).status,
                    MirrorUpdate.APPLIED,
                )
                self.assertEqual(
                    mirror.persist_and_apply(store, newest).status,
                    MirrorUpdate.DUPLICATE,
                )

                stale = self.event(
                    sequence=1,
                    odds="1.80",
                    observed_ts="2026-09-17T01:00:01+00:00",
                )
                result = mirror.persist_and_apply(store, stale)
                self.assertEqual(result.status, MirrorUpdate.STALE)
                self.assertEqual(len(store.events()), 2)
                self.assertEqual(
                    mirror.get(
                        "provider-a", "event-1", "market-1", "selection-1"
                    ).decimal_odds,
                    Decimal("2.20"),
                )
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                restored = MarketMirror.from_store(reopened)
                self.assertEqual(
                    restored.get(
                        "provider-a", "event-1", "market-1", "selection-1"
                    ).sequence,
                    2,
                )
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
