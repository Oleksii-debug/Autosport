from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.storage import SQLiteMarketStore


class StoragePayloadTextIntegrityTests(unittest.TestCase):
    @staticmethod
    def _event() -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": "event-1",
                "market_id": "match-winner",
                "selection_id": "player-a",
                "decimal_odds": "1.80",
                "observed_ts": "2026-09-14T00:00:01+00:00",
                "source_id": "source-x",
                "sequence": 1,
                "market_type": "winner",
                "status": "open",
                "source_ts": "2026-09-14T00:00:00+00:00",
                "ingest_ts": "2026-09-14T00:00:01+00:00",
                "metadata": {"provider": "fixture"},
            }
        )

    @staticmethod
    def _reformat_history_payload(db_path: Path, event: MarketEvent) -> str:
        connection = sqlite3.connect(db_path)
        try:
            persisted = connection.execute(
                "SELECT payload_json FROM market_events WHERE dedupe_key=?",
                (event.dedupe_key,),
            ).fetchone()
            if persisted is None:
                raise AssertionError("expected persisted market event")
            canonical_payload = persisted[0]
            raw = json.loads(canonical_payload)
            reformatted = json.dumps(
                raw,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            if reformatted == canonical_payload:
                raise AssertionError("test mutation must change persisted JSON text")
            connection.execute(
                "UPDATE market_events SET payload_json=? WHERE dedupe_key=?",
                (reformatted, event.dedupe_key),
            )
            connection.commit()
            return reformatted
        finally:
            connection.close()

    def test_live_history_read_rejects_semantically_equal_noncanonical_json_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            event = self._event()
            store = SQLiteMarketStore(db_path)
            self.assertTrue(store.append(event))

            self._reformat_history_payload(db_path, event)

            with self.assertRaisesRegex(
                ValueError,
                "stored market event payload is not canonical JSON text",
            ):
                store.events()
            store.close()

    def test_reopen_rejects_semantically_equal_noncanonical_history_without_repairing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.db"
            event = self._event()
            store = SQLiteMarketStore(db_path)
            self.assertTrue(store.append(event))
            store.close()

            reformatted = self._reformat_history_payload(db_path, event)

            with self.assertRaisesRegex(
                ValueError,
                "stored market event payload is not canonical JSON text",
            ):
                SQLiteMarketStore(db_path)

            check = sqlite3.connect(db_path)
            try:
                persisted = check.execute(
                    "SELECT payload_json FROM market_events WHERE dedupe_key=?",
                    (event.dedupe_key,),
                ).fetchone()
                self.assertIsNotNone(persisted)
                self.assertEqual(persisted[0], reformatted)
            finally:
                check.close()


if __name__ == "__main__":
    unittest.main()
