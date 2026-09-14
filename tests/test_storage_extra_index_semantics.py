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


class StorageExtraIndexSemanticTests(unittest.TestCase):
    @staticmethod
    def _create_canonical_tables(connection: sqlite3.Connection) -> None:
        connection.execute(MARKET_TABLE)
        connection.execute(CURRENT_TABLE)

    def test_extra_nonunique_index_with_custom_collation_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.create_collation(
                    "CUSTOM",
                    lambda left, right: (left > right) - (left < right),
                )
                self._create_canonical_tables(connection)
                connection.execute(
                    "CREATE INDEX extra_custom_collation "
                    "ON market_events(event_id COLLATE CUSTOM)"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "extra non-unique index extra_custom_collation is not semantically inert",
            ):
                SQLiteMarketStore(db_path)

    def test_extra_expression_index_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = sqlite3.connect(db_path)
            try:
                self._create_canonical_tables(connection)
                connection.execute(
                    "CREATE INDEX extra_expression ON market_events(lower(event_id))"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "extra non-unique index extra_expression is not semantically inert",
            ):
                SQLiteMarketStore(db_path)

    def test_extra_partial_index_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            connection = sqlite3.connect(db_path)
            try:
                self._create_canonical_tables(connection)
                connection.execute(
                    "CREATE INDEX extra_partial ON market_events(event_id) WHERE sequence > 0"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "extra non-unique index extra_partial is not semantically inert",
            ):
                SQLiteMarketStore(db_path)


if __name__ == "__main__":
    unittest.main()
