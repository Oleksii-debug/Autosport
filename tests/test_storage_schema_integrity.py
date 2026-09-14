import sqlite3
import tempfile
import unittest
from pathlib import Path

from autosport.storage import SQLiteMarketStore


MARKET_TABLE = """
CREATE TABLE market_events (
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
)
"""
CURRENT_TABLE = """
CREATE TABLE current_quotes (
    quote_key TEXT PRIMARY KEY,
    observed_ts TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_json TEXT NOT NULL
)
"""
CANONICAL_INDEXES = {
    "idx_market_events_order": ("observed_ts", "sequence"),
    "idx_market_events_event": ("event_id", "observed_ts", "sequence"),
    "idx_market_events_quote": ("quote_key", "observed_ts", "sequence"),
}


class StorageSchemaIntegrityTests(unittest.TestCase):
    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        return sqlite3.connect(path)

    @staticmethod
    def _create_canonical_tables(connection: sqlite3.Connection) -> None:
        connection.execute(MARKET_TABLE)
        connection.execute(CURRENT_TABLE)
        connection.commit()

    @staticmethod
    def _index_columns(connection: sqlite3.Connection, name: str) -> tuple[str, ...]:
        rows = connection.execute(f'PRAGMA index_xinfo("{name}")').fetchall()
        return tuple(row[2] for row in rows if row[5] == 1)

    def test_new_store_has_canonical_tables_and_secondary_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            store = SQLiteMarketStore(db_path)
            try:
                market_xinfo = store.connection.execute(
                    'PRAGMA table_xinfo("market_events")'
                ).fetchall()
                current_xinfo = store.connection.execute(
                    'PRAGMA table_xinfo("current_quotes")'
                ).fetchall()
                self.assertEqual(market_xinfo[0][1:7], ("dedupe_key", "TEXT", 0, None, 1, 0))
                self.assertEqual(current_xinfo[0][1:7], ("quote_key", "TEXT", 0, None, 1, 0))
                for name, columns in CANONICAL_INDEXES.items():
                    self.assertEqual(self._index_columns(store.connection, name), columns)
            finally:
                store.close()

    def test_existing_market_table_without_primary_key_fails_before_creating_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = self._connect(db_path)
            try:
                connection.execute(
                    MARKET_TABLE.replace("dedupe_key TEXT PRIMARY KEY", "dedupe_key TEXT")
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "market_events schema is not canonical: column definition mismatch",
            ):
                SQLiteMarketStore(db_path)

            check = self._connect(db_path)
            try:
                current = check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='current_quotes'"
                ).fetchone()
                self.assertIsNone(current)
            finally:
                check.close()

            # Constructor failure must release the SQLite handle on Windows as well.
            db_path.unlink()
            self.assertFalse(db_path.exists())

    def test_malformed_projection_schema_fails_before_projection_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = self._connect(db_path)
            try:
                connection.execute(MARKET_TABLE)
                connection.execute(
                    CURRENT_TABLE.replace("quote_key TEXT PRIMARY KEY", "quote_key TEXT")
                )
                connection.execute(
                    "INSERT INTO current_quotes(quote_key,observed_ts,sequence,payload_json) VALUES (?,?,?,?)",
                    ("sentinel", "2026-01-01T00:00:00+00:00", 1, "{}"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "current_quotes schema is not canonical: column definition mismatch",
            ):
                SQLiteMarketStore(db_path)

            check = self._connect(db_path)
            try:
                rows = check.execute("SELECT quote_key FROM current_quotes").fetchall()
                self.assertEqual(rows, [("sentinel",)])
            finally:
                check.close()

    def test_orphan_projection_without_authoritative_history_fails_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = self._connect(db_path)
            try:
                connection.execute(CURRENT_TABLE)
                connection.execute(
                    "INSERT INTO current_quotes(quote_key,observed_ts,sequence,payload_json) VALUES (?,?,?,?)",
                    ("sentinel", "2026-01-01T00:00:00+00:00", 1, "{}"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "authoritative history table is missing"):
                SQLiteMarketStore(db_path)

            check = self._connect(db_path)
            try:
                history = check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='market_events'"
                ).fetchone()
                rows = check.execute("SELECT quote_key FROM current_quotes").fetchall()
                self.assertIsNone(history)
                self.assertEqual(rows, [("sentinel",)])
            finally:
                check.close()

    def test_nonempty_projection_with_empty_history_fails_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = self._connect(db_path)
            try:
                self._create_canonical_tables(connection)
                connection.execute(
                    "INSERT INTO current_quotes(quote_key,observed_ts,sequence,payload_json) VALUES (?,?,?,?)",
                    ("sentinel", "2026-01-01T00:00:00+00:00", 1, "{}"),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "history is empty while current_quotes projection is non-empty",
            ):
                SQLiteMarketStore(db_path)

            check = self._connect(db_path)
            try:
                history_rows = check.execute("SELECT dedupe_key FROM market_events").fetchall()
                projection_rows = check.execute(
                    "SELECT quote_key,observed_ts,sequence,payload_json FROM current_quotes"
                ).fetchall()
                self.assertEqual(history_rows, [])
                self.assertEqual(
                    projection_rows,
                    [("sentinel", "2026-01-01T00:00:00+00:00", 1, "{}")],
                )
            finally:
                check.close()

    def test_trigger_on_canonical_table_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = self._connect(db_path)
            try:
                self._create_canonical_tables(connection)
                connection.execute(
                    "CREATE TRIGGER mutate_market AFTER INSERT ON market_events BEGIN "
                    "UPDATE market_events SET source_id='tampered' WHERE dedupe_key=NEW.dedupe_key; END"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "triggers are not allowed"):
                SQLiteMarketStore(db_path)

    def test_extra_unique_index_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = self._connect(db_path)
            try:
                self._create_canonical_tables(connection)
                connection.execute("CREATE UNIQUE INDEX unexpected_unique ON market_events(event_id)")
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "extra UNIQUE index unexpected_unique"):
                SQLiteMarketStore(db_path)

    def test_hidden_collation_semantics_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = self._connect(db_path)
            try:
                connection.execute(
                    MARKET_TABLE.replace(
                        "event_id TEXT NOT NULL",
                        "event_id TEXT COLLATE NOCASE NOT NULL",
                    )
                )
                connection.execute(CURRENT_TABLE)
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "semantic table constraint"):
                SQLiteMarketStore(db_path)

    def test_foreign_key_semantics_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = self._connect(db_path)
            try:
                connection.execute("CREATE TABLE parent_events(event_id TEXT PRIMARY KEY)")
                connection.execute(
                    MARKET_TABLE.replace(
                        "event_id TEXT NOT NULL",
                        "event_id TEXT NOT NULL REFERENCES parent_events(event_id)",
                    )
                )
                connection.execute(CURRENT_TABLE)
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "semantic table constraint|foreign keys are not allowed"):
                SQLiteMarketStore(db_path)

    def test_missing_secondary_indexes_are_recreated_without_removing_extra_nonunique_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = self._connect(db_path)
            try:
                self._create_canonical_tables(connection)
                connection.execute("CREATE INDEX extra_event_only ON market_events(event_id)")
                connection.commit()
            finally:
                connection.close()

            store = SQLiteMarketStore(db_path)
            try:
                names = {
                    row[1]
                    for row in store.connection.execute(
                        'PRAGMA index_list("market_events")'
                    ).fetchall()
                }
                self.assertIn("extra_event_only", names)
                for name, columns in CANONICAL_INDEXES.items():
                    self.assertIn(name, names)
                    self.assertEqual(self._index_columns(store.connection, name), columns)
            finally:
                store.close()

    def test_wrong_nonunique_canonical_index_is_safely_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = self._connect(db_path)
            try:
                self._create_canonical_tables(connection)
                connection.execute(
                    "CREATE INDEX idx_market_events_order ON market_events(event_id)"
                )
                connection.execute(
                    "CREATE INDEX idx_market_events_event ON market_events(event_id, observed_ts, sequence)"
                )
                connection.execute(
                    "CREATE INDEX idx_market_events_quote ON market_events(quote_key, observed_ts, sequence)"
                )
                connection.commit()
            finally:
                connection.close()

            store = SQLiteMarketStore(db_path)
            try:
                self.assertEqual(
                    self._index_columns(store.connection, "idx_market_events_order"),
                    ("observed_ts", "sequence"),
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
