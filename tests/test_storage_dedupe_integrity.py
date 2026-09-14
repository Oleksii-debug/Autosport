import sqlite3
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

    @staticmethod
    def _tamper(db_path: Path, column: str, value: object) -> None:
        allowed = {
            "dedupe_key",
            "quote_key",
            "event_id",
            "market_id",
            "selection_id",
            "decimal_odds",
            "observed_ts",
            "source_id",
            "sequence",
        }
        if column not in allowed:
            raise ValueError("unsupported test tamper column")
        with sqlite3.connect(db_path) as connection:
            connection.execute(f"UPDATE market_events SET {column}=?", (value,))

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
            with self.assertRaisesRegex(ValueError, "stored market event payload is not canonical"):
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

    def test_reopen_fails_closed_on_redundant_history_column_tamper(self):
        tamper_cases = (
            ("dedupe_key", "tampered-dedupe"),
            ("quote_key", "tampered|quote|key"),
            ("event_id", "tampered-event"),
            ("market_id", "tampered-market"),
            ("selection_id", "tampered-selection"),
            ("decimal_odds", "9.99"),
            ("observed_ts", "2026-01-01T00:00:09+00:00"),
            ("source_id", "tampered-source"),
            ("sequence", 99),
        )

        for column, value in tamper_cases:
            with self.subTest(column=column), tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "market.db"
                store = SQLiteMarketStore(db_path)
                self.assertTrue(store.append(self._event()))
                store.close()

                self._tamper(db_path, column, value)

                with self.assertRaisesRegex(
                    ValueError,
                    rf"market event history row identity mismatch: {column}",
                ):
                    SQLiteMarketStore(db_path)

    def test_tampered_dedupe_cannot_reopen_and_admit_duplicate_canonical_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            event = self._event()
            store = SQLiteMarketStore(db_path)
            self.assertTrue(store.append(event))
            store.close()

            self._tamper(db_path, "dedupe_key", "tampered-dedupe")

            with self.assertRaisesRegex(
                ValueError,
                "market event history row identity mismatch: dedupe_key",
            ):
                SQLiteMarketStore(db_path)

            with sqlite3.connect(db_path) as connection:
                count = connection.execute("SELECT COUNT(*) FROM market_events").fetchone()[0]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
