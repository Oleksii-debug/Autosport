import json
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
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(f"UPDATE market_events SET {column}=?", (value,))
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _tamper_current(db_path: Path, column: str, value: object) -> None:
        allowed = {"quote_key", "observed_ts", "sequence"}
        if column not in allowed:
            raise ValueError("unsupported current projection tamper column")
        connection = sqlite3.connect(db_path)
        try:
            connection.execute(f"UPDATE current_quotes SET {column}=?", (value,))
            connection.commit()
        finally:
            connection.close()

    def _assert_invalid_event_not_persisted(self, invalid: MarketEvent) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            with self.assertRaisesRegex(ValueError, "market event payload is not canonical"):
                store.append(invalid)

            count = store.connection.execute("SELECT COUNT(*) FROM market_events").fetchone()[0]
            self.assertEqual(count, 0)
            self.assertEqual(store.current(), {})
            store.close()

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

    def test_noncanonical_incoming_event_fails_before_persistence(self):
        invalid = replace(self._event(event_id="7"), event_id=7)  # type: ignore[arg-type]
        self._assert_invalid_event_not_persisted(invalid)

    def test_tuple_metadata_fails_before_json_type_drift_can_persist(self):
        invalid = replace(
            self._event(),
            metadata={"coordinates": (1, 2)},  # type: ignore[dict-item]
        )
        self._assert_invalid_event_not_persisted(invalid)

    def test_non_string_metadata_key_fails_before_json_key_coercion_can_persist(self):
        invalid = replace(
            self._event(),
            metadata={7: "seven"},  # type: ignore[dict-item]
        )
        self._assert_invalid_event_not_persisted(invalid)

    def test_non_finite_metadata_number_fails_before_persistence(self):
        invalid = replace(self._event(), metadata={"signal": float("inf")})
        self._assert_invalid_event_not_persisted(invalid)

    def test_noncanonical_event_rolls_back_earlier_batch_insert(self):
        valid = self._event(event_id="e2", sequence=2)
        invalid = replace(
            self._event(event_id="7", sequence=3),
            event_id=7,  # type: ignore[arg-type]
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            with self.assertRaisesRegex(ValueError, "market event payload is not canonical"):
                store.append_batch_accepted([valid, invalid])

            self.assertEqual(store.events(), [])
            self.assertEqual(store.current(), {})
            store.close()

    def test_json_type_drift_event_rolls_back_earlier_batch_insert(self):
        valid = self._event(event_id="e2", sequence=2)
        invalid = replace(
            self._event(event_id="e3", sequence=3),
            metadata={"coordinates": (1, 2)},  # type: ignore[dict-item]
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            with self.assertRaisesRegex(ValueError, "market event payload is not canonical"):
                store.append_batch_accepted([valid, invalid])

            self.assertEqual(store.events(), [])
            self.assertEqual(store.current(), {})
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

    def test_corrupt_database_constructor_releases_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            corrupt_bytes = b"not-a-sqlite-database"
            db_path.write_bytes(corrupt_bytes)

            with self.assertRaises(sqlite3.DatabaseError):
                SQLiteMarketStore(db_path)

            self.assertEqual(db_path.read_bytes(), corrupt_bytes)
            db_path.unlink()
            self.assertFalse(db_path.exists())

    def test_current_rejects_redundant_projection_column_tamper(self):
        tamper_cases = (
            ("quote_key", "tampered|quote|key"),
            ("observed_ts", "2026-01-01T00:00:09+00:00"),
            ("sequence", 99),
        )

        for column, value in tamper_cases:
            with self.subTest(column=column), tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "market.db"
                store = SQLiteMarketStore(db_path)
                self.assertTrue(store.append(self._event()))

                self._tamper_current(db_path, column, value)

                with self.assertRaisesRegex(
                    ValueError,
                    rf"current quote projection row identity mismatch: {column}",
                ):
                    store.current()
                store.close()

    def test_current_rejects_noncanonical_projection_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            event = self._event()
            store = SQLiteMarketStore(db_path)
            self.assertTrue(store.append(event))

            connection = sqlite3.connect(db_path)
            try:
                payload_json = connection.execute(
                    "SELECT payload_json FROM current_quotes WHERE quote_key=?",
                    (event.quote_key,),
                ).fetchone()[0]
                raw = json.loads(payload_json)
                raw["event_id"] = 7
                connection.execute(
                    "UPDATE current_quotes SET payload_json=? WHERE quote_key=?",
                    (json.dumps(raw, sort_keys=True, separators=(",", ":")), event.quote_key),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "current quote projection payload is not canonical"):
                store.current()
            store.close()

    def test_corrupt_projection_cannot_suppress_legitimate_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            first = self._event()
            second = self._event(
                observed_ts="2026-01-01T00:00:02+00:00",
                sequence=2,
                ingest_ts="2026-01-01T00:00:02+00:00",
            )
            store = SQLiteMarketStore(db_path)
            self.assertTrue(store.append(first))

            connection = sqlite3.connect(db_path)
            try:
                payload_json = connection.execute(
                    "SELECT payload_json FROM current_quotes WHERE quote_key=?",
                    (first.quote_key,),
                ).fetchone()[0]
                raw = json.loads(payload_json)
                raw["observed_ts"] = "2099-01-01T00:00:00+00:00"
                connection.execute(
                    "UPDATE current_quotes SET payload_json=? WHERE quote_key=?",
                    (json.dumps(raw, sort_keys=True, separators=(",", ":")), first.quote_key),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "current quote projection row identity mismatch: observed_ts",
            ):
                store.append(second)

            self.assertEqual(store.events(), [first])
            with self.assertRaisesRegex(
                ValueError,
                "current quote projection row identity mismatch: observed_ts",
            ):
                store.current()
            store.close()

            reopened = SQLiteMarketStore(db_path)
            self.assertEqual(reopened.events(), [first])
            self.assertEqual(reopened.current()[first.quote_key], first)
            reopened.close()

    def test_reopen_rejects_duplicate_keys_in_persisted_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            event = self._event()
            store = SQLiteMarketStore(db_path)
            self.assertTrue(store.append(event))
            store.close()

            connection = sqlite3.connect(db_path)
            try:
                payload = connection.execute(
                    "SELECT payload_json FROM market_events WHERE dedupe_key=?",
                    (event.dedupe_key,),
                ).fetchone()[0]
                marker = '"event_id":"e1"'
                self.assertIn(marker, payload)
                ambiguous = payload.replace(
                    marker,
                    '"event_id":"tampered","event_id":"e1"',
                    1,
                )
                connection.execute(
                    "UPDATE market_events SET payload_json=? WHERE dedupe_key=?",
                    (ambiguous, event.dedupe_key),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "duplicate object key: event_id"):
                SQLiteMarketStore(db_path)

    def test_reopen_rejects_non_finite_persisted_json_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            event = self._event()
            store = SQLiteMarketStore(db_path)
            self.assertTrue(store.append(event))
            store.close()

            connection = sqlite3.connect(db_path)
            try:
                payload = connection.execute(
                    "SELECT payload_json FROM market_events WHERE dedupe_key=?",
                    (event.dedupe_key,),
                ).fetchone()[0]
                marker = '"metadata":{}'
                self.assertIn(marker, payload)
                nonstandard = payload.replace(marker, '"metadata":{"signal":Infinity}', 1)
                connection.execute(
                    "UPDATE market_events SET payload_json=? WHERE dedupe_key=?",
                    (nonstandard, event.dedupe_key),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "non-finite JSON number: Infinity"):
                SQLiteMarketStore(db_path)

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

            connection = sqlite3.connect(db_path)
            try:
                count = connection.execute("SELECT COUNT(*) FROM market_events").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
