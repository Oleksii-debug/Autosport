from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore


class CollectorScheduleClockDiscontinuityTests(unittest.TestCase):
    def _store_with_schedule(self, root: str) -> CollectorDeltaStore:
        store = CollectorDeltaStore(Path(root) / "collector.db")
        store._ensure_collector_schedule(
            source_id="source-x",
            run_id="run-1",
            stream_epoch="epoch-1",
            anchor_at="2026-01-01T00:00:00+00:00",
            interval_seconds=10,
        )
        return store

    def test_start_before_frozen_due_at_is_rejected_without_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_schedule(tmp)

            with self.assertRaisesRegex(ValueError, "precede frozen due_at"):
                store._begin_scheduled_collector_cycle(
                    source_id="source-x",
                    run_id="run-1",
                    stream_epoch="epoch-1",
                    slot_ordinal=0,
                    due_at="2026-01-01T00:00:00+00:00",
                    attempted_at="2025-12-31T23:59:59+00:00",
                )

            evidence = store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=0,
            )
            self.assertEqual(evidence["bound_start_count"], 0)
            self.assertEqual(evidence["missing_start_count"], 1)

    def test_later_slot_start_time_cannot_regress_after_late_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_schedule(tmp)
            first = store._next_collector_schedule_slot(
                source_id="source-x",
                run_id="run-1",
            )
            store._begin_scheduled_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                slot_ordinal=first["slot_ordinal"],
                due_at=first["due_at"],
                attempted_at="2026-01-01T00:00:35+00:00",
            )
            second = store._next_collector_schedule_slot(
                source_id="source-x",
                run_id="run-1",
            )

            with self.assertRaisesRegex(ValueError, "START time cannot regress"):
                store._begin_scheduled_collector_cycle(
                    source_id="source-x",
                    run_id="run-1",
                    stream_epoch="epoch-1",
                    slot_ordinal=second["slot_ordinal"],
                    due_at=second["due_at"],
                    attempted_at="2026-01-01T00:00:20+00:00",
                )

            cycles = store.collector_cycle_evidence(
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            self.assertEqual(len(cycles), 1)
            evidence = store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(evidence["bound_start_count"], 1)
            self.assertEqual(evidence["missing_start_count"], 1)
            self.assertEqual(
                evidence["slots"][0]["attempted_at"],
                "2026-01-01T00:00:35+00:00",
            )
            self.assertIsNone(evidence["slots"][1]["attempted_at"])


if __name__ == "__main__":
    unittest.main()
