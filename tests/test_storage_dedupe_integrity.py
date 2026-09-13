import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.storage import SQLiteMarketStore


class StorageDedupeIntegrityTests(unittest.TestCase):
    @staticmethod
    def _event(
        *,
        event_id: str = "e1",
        odds: str = "1.80",
        observed_ts: str = "2026-01-01T00:00:01+00:00",
        source_ts: str = "2026-01-01T00:00:00+00:00",
        sequence: int = 1,
        ingest_ts: str = "2026-01-01T00:00:01+00:00",
    ) -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": event_id,
                "market_id": "winner",
                "selection_id": "player-a",
                "decimal_odds": odds,
                "observed_ts": observed_ts,
                "source_id": "source-x",
                "sequence": sequence,
                "market_type": "winner",
                "status": "open",
                "source_ts": source_ts,
                "ingest_ts": ingest_ts,
            }
        )

    def test_exact_source_duplicate_is_idempotent_across_local_observation_times(self):
        first = self._event(
            observed_ts="2026-01-01T00:00:01+00:00",
            ingest_ts="2026-01-01T00:00:01+00:00",
        )
        duplicate = self._event(
            observed_ts="2026-01-01T00:00:02+00:00",
            ingest_ts="2026-01-01T00:00:02+00:00",
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            self.assertTrue(store.append(first))
            self.assertFalse(store.append(duplicate))
            self.assertEqual(len(store.events()), 1)
            self.assertEqual(store.events()[0].observed_ts, first.observed_ts)
            self.assertEqual(store.events()[0].ingest_ts, first.ingest_ts)
            store.close()

    def test_same_dedupe_key_with_different_odds_fails_closed(self):
        first = self._event(odds="1.80")
        conflicting = self._event(odds="2.10")

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            self.assertTrue(store.append(first))
            with self.assertRaisesRegex(ValueError, "conflicting duplicate market event identity"):
                store.append(conflicting)

            persisted = store.events()
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0].decimal_odds, first.decimal_odds)
            self.assertEqual(store.current()[first.quote_key].decimal_odds, first.decimal_odds)
            store.close()

    def test_same_dedupe_key_with_different_source_time_fails_closed(self):
        first = self._event(source_ts="2026-01-01T00:00:00+00:00")
        conflicting = self._event(source_ts="2026-01-01T00:00:05+00:00")

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            self.assertTrue(store.append(first))
            with self.assertRaisesRegex(ValueError, "conflicting duplicate market event identity"):
                store.append(conflicting)
            self.assertEqual(store.events(), [first])
            store.close()

    def test_raw_persisted_type_disagreement_is_not_normalized_away(self):
        first = replace(self._event(), event_id=7)  # type: ignore[arg-type]
        conflicting = self._event(event_id="7")
        self.assertEqual(first.dedupe_key, conflicting.dedupe_key)

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            self.assertTrue(store.append(first))
            with self.assertRaisesRegex(ValueError, "conflicting duplicate market event identity"):
                store.append(conflicting)
            self.assertEqual(len(store.events()), 1)
            store.close()

    def test_conflicting_duplicate_rolls_back_earlier_insert_in_batch(self):
        existing = self._event()
        new_event = self._event(event_id="e2", sequence=2)
        conflicting = self._event(odds="2.25")

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            self.assertTrue(store.append(existing))

            with self.assertRaisesRegex(ValueError, "conflicting duplicate market event identity"):
                store.append_batch_accepted([new_event, conflicting])

            self.assertEqual([event.event_id for event in store.events()], ["e1"])
            self.assertNotIn(new_event.quote_key, store.current())
            self.assertEqual(store.current()[existing.quote_key].decimal_odds, existing.decimal_odds)
            store.close()


if __name__ == "__main__":
    unittest.main()
