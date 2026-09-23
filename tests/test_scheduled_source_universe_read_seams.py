from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autosport.causal_collector import CollectorDeltaStore
from autosport.scheduled_source_universe import resolve_scheduled_source_universe
from autosport.source_universe_commitment import build_source_universe_commitment


SOURCE_ID = "source-x"
RUN_ID = "run-1"
STREAM_EPOCH = "epoch-1"
ANCHOR = "2026-09-22T00:00:00+00:00"


def _unbounded_success(path: Path):
    store = CollectorDeltaStore(path)
    store._ensure_collector_schedule(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
        stream_epoch=STREAM_EPOCH,
        anchor_at=ANCHOR,
        interval_seconds=10,
        max_items=250,
    )
    slot = store._next_collector_schedule_slot(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
    )
    cycle_seq = store._begin_scheduled_collector_cycle(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
        stream_epoch=STREAM_EPOCH,
        max_items=250,
        slot_ordinal=slot["slot_ordinal"],
        due_at=slot["due_at"],
        attempted_at=slot["due_at"],
    )
    store._finish_collector_cycle(
        source_id=SOURCE_ID,
        cycle_seq=cycle_seq,
        status="SUCCESS",
        completed_at="2026-09-22T00:00:01+00:00",
        catalog_changes=(),
        observed_delta_ids=(),
        committed_delta_ids=(),
        duplicate_delta_ids=(),
    )
    candidate = build_source_universe_commitment(
        store,
        expected_store_path=path,
        source_id=SOURCE_ID,
        start_cycle_seq=cycle_seq,
        end_cycle_seq=cycle_seq,
    )
    return store, candidate


def _resolve(store: CollectorDeltaStore, path: Path, candidate):
    return resolve_scheduled_source_universe(
        store,
        candidate,
        expected_store_path=path,
        expected_source_id=SOURCE_ID,
        expected_run_id=RUN_ID,
        expected_start_slot_ordinal=0,
        expected_end_slot_ordinal=0,
    )


class ScheduledSourceUniverseReadSeamTests(unittest.TestCase):
    def test_instance_rebound_evaluation_window_cannot_launder_unbounded_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store, candidate = _unbounded_success(path)

            store._schedule_evaluation_window = lambda _start, _end: (0, 0)

            with self.assertRaisesRegex(TypeError, "instance-rebound"):
                _resolve(store, path, candidate)

    def test_class_rebound_evaluation_window_cannot_launder_unbounded_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store, candidate = _unbounded_success(path)

            with mock.patch.object(
                CollectorDeltaStore,
                "_schedule_evaluation_window",
                staticmethod(lambda _start, _end: (0, 0)),
            ):
                with self.assertRaisesRegex(
                    Exception,
                    "class-rebound",
                ):
                    _resolve(store, path, candidate)


if __name__ == "__main__":
    unittest.main()
