from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.scheduled_source_universe import (
    ScheduledSourceUniverseError,
    resolve_scheduled_source_universe,
)
from autosport.source_universe_commitment import (
    build_source_universe_commitment,
)


SOURCE_ID = "source-x"
RUN_ID = "run-1"
STREAM_EPOCH = "epoch-1"
ANCHOR = "2026-09-22T00:00:00+00:00"


def _ensure_schedule(
    store: CollectorDeltaStore,
    *,
    end_slot: int | None = 0,
) -> None:
    store._ensure_collector_schedule(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
        stream_epoch=STREAM_EPOCH,
        anchor_at=ANCHOR,
        interval_seconds=10,
        max_items=250,
        evaluation_start_slot_ordinal=(0 if end_slot is not None else None),
        evaluation_end_slot_ordinal=end_slot,
    )


def _start_scheduled(
    store: CollectorDeltaStore,
    *,
    ordinal: int,
    attempted_at: str | None = None,
) -> int:
    slot = store._next_collector_schedule_slot(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
    )
    if slot["slot_ordinal"] != ordinal:
        raise AssertionError(
            f"expected slot {ordinal}, got {slot['slot_ordinal']}"
        )
    return store._begin_scheduled_collector_cycle(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
        stream_epoch=STREAM_EPOCH,
        max_items=250,
        slot_ordinal=slot["slot_ordinal"],
        due_at=slot["due_at"],
        attempted_at=attempted_at or slot["due_at"],
    )


def _finish(
    store: CollectorDeltaStore,
    cycle_seq: int,
    *,
    status: str = "SUCCESS",
    completed_at: str = "2026-09-22T00:00:30+00:00",
) -> None:
    store._finish_collector_cycle(
        source_id=SOURCE_ID,
        cycle_seq=cycle_seq,
        status=status,
        completed_at=completed_at,
        catalog_changes=(),
        observed_delta_ids=(),
        committed_delta_ids=(),
        duplicate_delta_ids=(),
        error_code=None if status == "SUCCESS" else status.lower(),
    )


def _candidate(
    store: CollectorDeltaStore,
    path: Path,
    *,
    start_cycle_seq: int,
    end_cycle_seq: int,
):
    return build_source_universe_commitment(
        store,
        expected_store_path=path,
        source_id=SOURCE_ID,
        start_cycle_seq=start_cycle_seq,
        end_cycle_seq=end_cycle_seq,
    )


def _resolve(
    store: CollectorDeltaStore,
    path: Path,
    candidate,
    *,
    start_slot: int,
    end_slot: int,
):
    return resolve_scheduled_source_universe(
        store,
        candidate,
        expected_store_path=path,
        expected_source_id=SOURCE_ID,
        expected_run_id=RUN_ID,
        expected_start_slot_ordinal=start_slot,
        expected_end_slot_ordinal=end_slot,
    )


class ScheduledSourceUniverseTests(unittest.TestCase):
    def test_two_zero_result_successes_compose_exact_schedule_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store, end_slot=1)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first)
            second = _start_scheduled(store, ordinal=1)
            _finish(store, second)

            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=second,
            )
            resolution = _resolve(
                store,
                path,
                candidate,
                start_slot=0,
                end_slot=1,
            )

            self.assertTrue(resolution.scheduled_start_coverage_complete)
            self.assertTrue(resolution.observation_ledger_complete)
            self.assertTrue(resolution.scheduled_provider_observation_complete)
            self.assertEqual(resolution.expected_slot_count, 2)
            self.assertEqual(resolution.cycle_sequences, (first, second))
            self.assertEqual(resolution.late_start_count, 0)
            self.assertFalse(resolution.external_provider_universe_complete)
            self.assertFalse(resolution.promotion_ready)
            self.assertEqual(len(resolution.resolution_sha256), 64)

    def test_missing_due_slot_fails_closed_before_cycle_window_can_be_laundered(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store, end_slot=1)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first)
            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )

            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "missing canonical START coverage",
            ):
                _resolve(
                    store,
                    path,
                    candidate,
                    start_slot=0,
                    end_slot=1,
                )

    def test_frozen_two_slot_window_rejects_caller_selected_shorter_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store, end_slot=1)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first)
            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )

            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "prospectively frozen evaluation window",
            ):
                _resolve(
                    store,
                    path,
                    candidate,
                    start_slot=0,
                    end_slot=0,
                )

    def test_unbounded_schedule_cannot_mint_positive_scientific_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store, end_slot=None)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first)
            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )

            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "lacks prospectively frozen evaluation window",
            ):
                _resolve(
                    store,
                    path,
                    candidate,
                    start_slot=0,
                    end_slot=0,
                )

    def test_pending_start_is_schedule_covered_but_not_provider_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store)
            first = _start_scheduled(store, ordinal=0)
            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )

            resolution = _resolve(
                store,
                path,
                candidate,
                start_slot=0,
                end_slot=0,
            )

            self.assertTrue(resolution.scheduled_start_coverage_complete)
            self.assertFalse(resolution.observation_ledger_complete)
            self.assertFalse(resolution.scheduled_provider_observation_complete)

    def test_provider_failure_remains_explicit_negative_provider_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first, status="PROVIDER_UNAVAILABLE")
            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )

            resolution = _resolve(
                store,
                path,
                candidate,
                start_slot=0,
                end_slot=0,
            )

            self.assertTrue(resolution.scheduled_start_coverage_complete)
            self.assertTrue(resolution.observation_ledger_complete)
            self.assertFalse(resolution.scheduled_provider_observation_complete)

    def test_late_start_remains_visible_without_disappearing_from_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store)
            first = _start_scheduled(
                store,
                ordinal=0,
                attempted_at="2026-09-22T00:00:02+00:00",
            )
            _finish(
                store,
                first,
                completed_at="2026-09-22T00:00:03+00:00",
            )
            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )

            resolution = _resolve(
                store,
                path,
                candidate,
                start_slot=0,
                end_slot=0,
            )

            self.assertTrue(resolution.scheduled_start_coverage_complete)
            self.assertEqual(resolution.late_start_count, 1)
            self.assertFalse(resolution.scheduled_provider_observation_complete)

    def test_manual_cycle_between_scheduled_slots_prevents_exact_bijection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store, end_slot=1)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first)

            manual = store._begin_collector_cycle(
                source_id=SOURCE_ID,
                run_id="manual-run",
                stream_epoch=STREAM_EPOCH,
                attempted_at="2026-09-22T00:00:05+00:00",
            )
            _finish(
                store,
                manual,
                completed_at="2026-09-22T00:00:06+00:00",
            )

            second = _start_scheduled(
                store,
                ordinal=1,
                attempted_at="2026-09-22T00:00:10+00:00",
            )
            _finish(
                store,
                second,
                completed_at="2026-09-22T00:00:11+00:00",
            )
            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=second,
            )

            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "not one exact contiguous cycle window",
            ):
                _resolve(
                    store,
                    path,
                    candidate,
                    start_slot=0,
                    end_slot=1,
                )

    def test_favorable_smaller_cycle_commitment_cannot_satisfy_two_slot_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store, end_slot=1)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first)
            second = _start_scheduled(
                store,
                ordinal=1,
                attempted_at="2026-09-22T00:00:10+00:00",
            )
            _finish(
                store,
                second,
                completed_at="2026-09-22T00:00:11+00:00",
            )
            favorable = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )

            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "does not match scheduled cycle window",
            ):
                _resolve(
                    store,
                    path,
                    favorable,
                    start_slot=0,
                    end_slot=1,
                )

    def test_schedule_reader_instance_rebind_cannot_mint_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first)
            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )
            store.collector_schedule_evidence = lambda **_kwargs: {
                "forged": True
            }

            with self.assertRaisesRegex(TypeError, "instance-rebound"):
                _resolve(
                    store,
                    path,
                    candidate,
                    start_slot=0,
                    end_slot=0,
                )

    def test_schedule_max_items_instance_rebind_cannot_mint_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first)
            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )
            store._schedule_max_items = lambda _value: 250

            with self.assertRaisesRegex(TypeError, "instance-rebound"):
                _resolve(
                    store,
                    path,
                    candidate,
                    start_slot=0,
                    end_slot=0,
                )

    def test_store_path_rebind_to_decoy_database_fails_before_schedule_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            canonical_path = Path(tmp) / "canonical.db"
            decoy_path = Path(tmp) / "decoy.db"
            store = CollectorDeltaStore(canonical_path)
            _ensure_schedule(store)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first)
            candidate = _candidate(
                store,
                canonical_path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )

            decoy = CollectorDeltaStore(decoy_path)
            decoy._ensure_collector_schedule(
                source_id=SOURCE_ID,
                run_id=RUN_ID,
                stream_epoch=STREAM_EPOCH,
                anchor_at=ANCHOR,
                interval_seconds=10,
            max_items=250,
            )
            decoy_first = _start_scheduled(decoy, ordinal=0)
            _finish(decoy, decoy_first)
            store.path = decoy_path

            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "does not match product-expected authority path",
            ):
                _resolve(
                    store,
                    canonical_path,
                    candidate,
                    start_slot=0,
                    end_slot=0,
                )

    def test_repeat_resolution_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            _ensure_schedule(store)
            first = _start_scheduled(store, ordinal=0)
            _finish(store, first)
            candidate = _candidate(
                store,
                path,
                start_cycle_seq=first,
                end_cycle_seq=first,
            )

            first_resolution = _resolve(
                store,
                path,
                candidate,
                start_slot=0,
                end_slot=0,
            )
            second_resolution = _resolve(
                store,
                path,
                candidate,
                start_slot=0,
                end_slot=0,
            )

            self.assertEqual(
                first_resolution.to_dict(),
                second_resolution.to_dict(),
            )


if __name__ == "__main__":
    unittest.main()
