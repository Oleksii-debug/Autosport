import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore, StreamCheckpoint
from test_collector_delta_sqlite_store import make_delta


_TRIGGER = "collector_deltas_projection_immutable_v1"
_MARKER = "indexed_projection_integrity_v1"


class CollectorSQLiteDurabilityRepairTests(unittest.TestCase):
    def _write_legacy_store(self, path: Path) -> str:
        delta = make_delta()
        checkpoint = StreamCheckpoint(
            source_id="source-x",
            stream_epoch="epoch-1",
            last_cursor="1",
            last_position=1,
            last_delta_id="d1",
        )
        raw = {
            "schema_version": 1,
            "deltas": [delta.to_dict()],
            "streams": {"source-x|epoch-1": asdict(checkpoint)},
        }
        source = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ) + "\n"
        path.write_text(source, encoding="utf-8")
        return source

    def test_migration_loser_rechecks_winner_before_any_authority_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            source = self._write_legacy_store(path)

            winner = CollectorDeltaStore(path)
            second = make_delta(delta_id="d2", cursor_position=2)
            self.assertTrue(winner.append(second))

            # Model a delayed first-opener that reaches migration only after another
            # process has already won the authority switch and appended newer truth.
            # The migration method must re-read the canonical path inside its
            # cross-process lock and adopt the winner rather than rebuild/replace it.
            loser = object.__new__(CollectorDeltaStore)
            loser.path = path
            loser._migrate_legacy_json()

            reopened = CollectorDeltaStore(path)
            self.assertEqual(reopened.get("d1"), make_delta())
            self.assertEqual(reopened.get("d2"), second)
            self.assertEqual(
                path.with_name("collector.json.legacy-v1.json").read_text(
                    encoding="utf-8"
                ),
                source,
            )
            self.assertTrue(
                path.with_name(".collector.json.sqlite-migration.lock").exists()
            )

    def test_indexed_projection_columns_are_sql_immutable_after_qualification(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            delta = make_delta()
            CollectorDeltaStore(path).append(delta)

            with sqlite3.connect(path) as connection:
                marker = connection.execute(
                    "SELECT value FROM collector_meta WHERE key=?", (_MARKER,)
                ).fetchone()
                self.assertEqual(marker, ("1",))
                trigger = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name=?",
                    (_TRIGGER,),
                ).fetchone()
                self.assertEqual(trigger, (_TRIGGER,))
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE collector_deltas SET source_id=? WHERE delta_id=?",
                        ("forged-source", delta.delta_id),
                    )

            self.assertEqual(CollectorDeltaStore(path).get(delta.delta_id), delta)

    def test_index_projection_drift_fails_closed_against_canonical_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            delta = make_delta()
            store = CollectorDeltaStore(path)
            store.append(delta)

            # Simulate offline corruption that bypassed the SQL immutability trigger.
            # The digest-authenticated payload remains untouched; any redundant SQL
            # projection must still be reconciled before it can become evidence.
            with sqlite3.connect(path) as connection:
                connection.execute(f"DROP TRIGGER {_TRIGGER}")
                connection.execute(
                    "UPDATE collector_deltas SET cursor_position=? WHERE delta_id=?",
                    (99, delta.delta_id),
                )
                connection.commit()

            with self.assertRaisesRegex(
                ValueError,
                "indexed projection conflicts with payload: cursor_position",
            ):
                store.get(delta.delta_id)
            with self.assertRaisesRegex(
                ValueError,
                "indexed projection conflicts with payload: cursor_position",
            ):
                store.deltas_after_commit(source_id="source-x")

    def test_upgrade_scan_rejects_preexisting_projection_drift_before_marking_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            delta = make_delta()
            CollectorDeltaStore(path).append(delta)

            # Emulate a database written by the earlier branch head: no guard marker
            # or trigger, plus an already-diverged redundant projection.
            with sqlite3.connect(path) as connection:
                connection.execute(f"DROP TRIGGER {_TRIGGER}")
                connection.execute(
                    "DELETE FROM collector_meta WHERE key=?", (_MARKER,)
                )
                connection.execute(
                    "UPDATE collector_deltas SET revision_number=? WHERE delta_id=?",
                    (7, delta.delta_id),
                )
                connection.commit()

            with self.assertRaisesRegex(
                ValueError,
                "indexed projection conflicts with payload: revision_number",
            ):
                CollectorDeltaStore(path)


if __name__ == "__main__":
    unittest.main()
