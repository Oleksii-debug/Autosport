from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.storage import SQLiteMarketStore


class StoragePartialHistoryLossTests(unittest.TestCase):
    @staticmethod
    def _event(*, selection_id: str, sequence: int, observed_ts: str, odds: str) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="match-winner",
            selection_id=selection_id,
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            source_id="test-source",
            sequence=sequence,
            ingest_ts=observed_ts,
        )

    def test_partial_authoritative_history_loss_fails_without_projection_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            missing = self._event(
                selection_id="home",
                sequence=1,
                observed_ts="2026-09-14T00:00:00+00:00",
                odds="2.10",
            )
            surviving = self._event(
                selection_id="away",
                sequence=2,
                observed_ts="2026-09-14T00:00:01+00:00",
                odds="1.90",
            )

            store = SQLiteMarketStore(db_path)
            try:
                store.append_many((missing, surviving))
            finally:
                store.close()

            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "DELETE FROM market_events WHERE dedupe_key=?",
                    (missing.dedupe_key,),
                )
                connection.commit()
                history_before = connection.execute(
                    "SELECT dedupe_key,quote_key,payload_json FROM market_events ORDER BY dedupe_key"
                ).fetchall()
                projection_before = connection.execute(
                    "SELECT quote_key,observed_ts,sequence,payload_json FROM current_quotes ORDER BY quote_key"
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(len(history_before), 1)
            self.assertEqual(len(projection_before), 2)

            with self.assertRaisesRegex(
                ValueError,
                "projection event is missing from authoritative history",
            ):
                SQLiteMarketStore(db_path)

            check = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    check.execute(
                        "SELECT dedupe_key,quote_key,payload_json FROM market_events ORDER BY dedupe_key"
                    ).fetchall(),
                    history_before,
                )
                self.assertEqual(
                    check.execute(
                        "SELECT quote_key,observed_ts,sequence,payload_json FROM current_quotes ORDER BY quote_key"
                    ).fetchall(),
                    projection_before,
                )
            finally:
                check.close()

    def test_stale_projection_remains_repairable_when_exact_old_event_is_in_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            old = self._event(
                selection_id="home",
                sequence=1,
                observed_ts="2026-09-14T00:00:00+00:00",
                odds="2.10",
            )
            new = self._event(
                selection_id="home",
                sequence=2,
                observed_ts="2026-09-14T00:00:01+00:00",
                odds="2.20",
            )

            store = SQLiteMarketStore(db_path)
            try:
                store.append_many((old, new))
            finally:
                store.close()

            connection = sqlite3.connect(db_path)
            try:
                old_payload = connection.execute(
                    "SELECT payload_json FROM market_events WHERE dedupe_key=?",
                    (old.dedupe_key,),
                ).fetchone()
                self.assertIsNotNone(old_payload)
                connection.execute(
                    "UPDATE current_quotes SET observed_ts=?, sequence=?, payload_json=? WHERE quote_key=?",
                    (old.observed_ts, old.sequence, old_payload[0], old.quote_key),
                )
                connection.commit()
            finally:
                connection.close()

            repaired = SQLiteMarketStore(db_path)
            try:
                self.assertEqual(repaired.current()[new.quote_key], new)
            finally:
                repaired.close()


if __name__ == "__main__":
    unittest.main()
