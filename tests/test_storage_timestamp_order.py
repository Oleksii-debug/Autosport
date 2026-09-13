import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.domain import MarketEvent
from autosport.storage import SQLiteMarketStore


class StoragePhysicalTimeTests(unittest.TestCase):
    def _event(self, *, odds: str, observed_ts: str, sequence: int) -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": "e1",
                "market_id": "m1",
                "selection_id": "a",
                "decimal_odds": odds,
                "observed_ts": observed_ts,
                "source_id": "source",
                "sequence": sequence,
            }
        )

    def test_mixed_offsets_order_by_physical_instant_and_drive_current_projection(self):
        earlier = self._event(
            odds="1.8",
            observed_ts="2026-01-01T01:00:00+01:00",
            sequence=10,
        )
        later = self._event(
            odds="2.0",
            observed_ts="2026-01-01T00:30:00+00:00",
            sequence=1,
        )
        self.assertGreater(earlier.observed_ts, later.observed_ts)  # lexical order is intentionally wrong

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            self.assertEqual(store.append_many([earlier, later]), 2)
            self.assertEqual([event.decimal_odds for event in store.events()], [Decimal("1.8"), Decimal("2.0")])
            self.assertEqual(store.current()[earlier.quote_key].decimal_odds, Decimal("2.0"))
            store.close()

    def test_reopen_repairs_legacy_lexical_current_projection(self):
        earlier = self._event(
            odds="1.8",
            observed_ts="2026-01-01T01:00:00+01:00",
            sequence=10,
        )
        later = self._event(
            odds="2.0",
            observed_ts="2026-01-01T00:30:00+00:00",
            sequence=1,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.db"
            store = SQLiteMarketStore(path)
            store.append_many([earlier, later])
            legacy_payload = json.dumps(
                earlier.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            store.connection.execute(
                "UPDATE current_quotes SET observed_ts=?, sequence=?, payload_json=? WHERE quote_key=?",
                (earlier.observed_ts, earlier.sequence, legacy_payload, earlier.quote_key),
            )
            store.connection.commit()
            self.assertEqual(store.current()[earlier.quote_key].decimal_odds, Decimal("1.8"))
            store.close()

            reopened = SQLiteMarketStore(path)
            self.assertEqual(reopened.current()[earlier.quote_key].decimal_odds, Decimal("2.0"))
            reopened.close()

    def test_rebuild_takes_write_transaction_before_history_snapshot(self):
        original_connect = sqlite3.connect

        class SnapshotCheckingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.strip() == "SELECT payload_json FROM market_events" and not self.in_transaction:
                    raise AssertionError("current projection rebuild must lock writers before reading history")
                return super().execute(sql, parameters)

        def checked_connect(*args, **kwargs):
            kwargs["factory"] = SnapshotCheckingConnection
            return original_connect(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            with patch("autosport.storage.sqlite3.connect", side_effect=checked_connect):
                store = SQLiteMarketStore(Path(tmp) / "market.db")
                store.close()

    def test_naive_timestamp_fails_closed_before_persistence(self):
        naive = self._event(
            odds="1.8",
            observed_ts="2026-01-01T00:00:00",
            sequence=1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                store.append(naive)
            self.assertEqual(store.events(), [])
            self.assertEqual(store.current(), {})
            store.close()


if __name__ == "__main__":
    unittest.main()
