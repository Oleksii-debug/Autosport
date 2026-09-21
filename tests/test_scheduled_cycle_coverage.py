import sqlite3
import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.collector_service import CollectorServiceConfig
from autosport.scheduled_cycle_coverage import (
    FrozenAcquisitionSchedule,
    ScheduledCycleCoverage,
    ScheduledCycleCoverageError,
    freeze_acquisition_schedule,
    load_frozen_acquisition_schedule,
    resolve_scheduled_cycle_coverage,
)


def _config():
    return CollectorServiceConfig(
        max_items=10,
        poll_interval_seconds=10,
        retry_attempts=1,
        initial_backoff_seconds=1,
        max_backoff_seconds=1,
        jitter_fraction=0,
        max_store_bytes=1_000_000,
    )


def _freeze(store, *, source_id="source-x", scope=None, clock=None):
    return freeze_acquisition_schedule(
        store,
        source_id=source_id,
        adapter_id="adapter-v1",
        sport="table_tennis",
        query_scope=scope if scope is not None else {"league": "league-a"},
        schedule_policy_version="cadence-v1",
        window_start="2026-01-01T00:00:10+00:00",
        window_end="2026-01-01T00:00:40+00:00",
        campaign_id="campaign-1",
        protocol_id="protocol-1",
        collector_config=_config(),
        _clock=clock or (lambda: "2026-01-01T00:00:00+00:00"),
    )


def _start(store, at, *, source_id="source-x"):
    return store._begin_collector_cycle(
        source_id=source_id,
        run_id="run-1",
        stream_epoch="epoch-1",
        attempted_at=at,
    )


class ScheduledCycleCoverageTests(unittest.TestCase):
    def test_three_due_slots_require_three_canonical_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            schedule = _freeze(store)
            self.assertEqual(len(schedule.slots), 3)

            first = _start(store, "2026-01-01T00:00:10+00:00")
            store._finish_collector_cycle(
                source_id="source-x",
                cycle_seq=first,
                status="SUCCESS",
                completed_at="2026-01-01T00:00:11+00:00",
                catalog_changes=(),
                observed_delta_ids=(),
                committed_delta_ids=(),
                duplicate_delta_ids=(),
            )
            _start(store, "2026-01-01T00:00:20+00:00")
            third = _start(store, "2026-01-01T00:00:30+00:00")
            store._finish_collector_cycle(
                source_id="source-x",
                cycle_seq=third,
                status="PROVIDER_UNAVAILABLE",
                completed_at="2026-01-01T00:00:31+00:00",
                catalog_changes=(),
                observed_delta_ids=(),
                committed_delta_ids=(),
                duplicate_delta_ids=(),
                error_code="provider_unavailable",
            )

            coverage = resolve_scheduled_cycle_coverage(
                store, schedule_id=schedule.schedule_id
            )
            self.assertTrue(coverage.schedule_coverage_complete)
            self.assertEqual(coverage.started_slot_count, 3)
            self.assertEqual(coverage.started_cycle_count, 3)
            self.assertEqual(coverage.pending_cycle_count, 1)
            self.assertEqual(coverage.missing_slot_ids, ())
            self.assertEqual(coverage.duplicate_slot_ids, ())
            self.assertFalse(coverage.external_provider_universe_complete)
            self.assertFalse(coverage.promotion_ready)

    def test_missing_due_slot_stays_missing_even_with_adjacent_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            schedule = _freeze(store)
            _start(store, "2026-01-01T00:00:10+00:00")
            _start(store, "2026-01-01T00:00:30+00:00")

            coverage = resolve_scheduled_cycle_coverage(
                store, schedule_id=schedule.schedule_id
            )
            self.assertFalse(coverage.schedule_coverage_complete)
            self.assertEqual(coverage.started_slot_count, 2)
            self.assertEqual(
                coverage.missing_slot_ids,
                (schedule.slots[1].slot_id,),
            )

    def test_two_starts_in_one_due_slot_are_duplicate_execution_not_extra_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            schedule = _freeze(store)
            _start(store, "2026-01-01T00:00:10+00:00")
            _start(store, "2026-01-01T00:00:15+00:00")
            _start(store, "2026-01-01T00:00:20+00:00")
            _start(store, "2026-01-01T00:00:30+00:00")

            coverage = resolve_scheduled_cycle_coverage(
                store, schedule_id=schedule.schedule_id
            )
            self.assertFalse(coverage.schedule_coverage_complete)
            self.assertEqual(
                coverage.duplicate_slot_ids,
                (schedule.slots[0].slot_id,),
            )
            self.assertEqual(coverage.started_cycle_count, 4)
            self.assertEqual(coverage.started_slot_count, 3)

    def test_restart_reresolves_same_frozen_schedule_without_shifting_due_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            first = _freeze(store)
            second = _freeze(
                store,
                clock=lambda: "2026-01-01T00:01:00+00:00",
            )
            self.assertEqual(first.schedule_id, second.schedule_id)
            self.assertEqual(first.commitment_sha256, second.commitment_sha256)
            self.assertEqual(first.slots, second.slots)

    def test_new_post_outcome_schedule_is_rejected_instead_of_backdated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            with self.assertRaisesRegex(
                ScheduledCycleCoverageError, "frozen no later"
            ):
                _freeze(
                    store,
                    scope={"league": "different-post-outcome-scope"},
                    clock=lambda: "2026-01-01T00:00:25+00:00",
                )

    def test_scope_change_changes_schedule_identity_before_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            first = _freeze(store, scope={"league": "league-a"})
            second = _freeze(store, scope={"league": "league-b"})
            self.assertNotEqual(first.schedule_id, second.schedule_id)
            self.assertNotEqual(first.slots[0].slot_id, second.slots[0].slot_id)

    def test_other_source_start_cannot_satisfy_frozen_source_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            schedule = _freeze(store, source_id="source-x")
            _start(
                store,
                "2026-01-01T00:00:10+00:00",
                source_id="source-y",
            )
            coverage = resolve_scheduled_cycle_coverage(
                store, schedule_id=schedule.schedule_id
            )
            self.assertFalse(coverage.schedule_coverage_complete)
            self.assertEqual(len(coverage.missing_slot_ids), 3)
            self.assertEqual(coverage.started_cycle_count, 0)

    def test_cycle_at_window_end_cannot_be_shifted_back_into_last_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            schedule = _freeze(store)
            _start(store, "2026-01-01T00:00:10+00:00")
            _start(store, "2026-01-01T00:00:20+00:00")
            _start(store, "2026-01-01T00:00:40+00:00")

            coverage = resolve_scheduled_cycle_coverage(
                store, schedule_id=schedule.schedule_id
            )
            self.assertFalse(coverage.schedule_coverage_complete)
            self.assertEqual(
                coverage.missing_slot_ids,
                (schedule.slots[2].slot_id,),
            )

    def test_frozen_schedule_rows_are_sql_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            schedule = _freeze(store)

            connection = sqlite3.connect(path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "UPDATE collector_frozen_schedule_slots_v1 "
                        "SET due_at='2030-01-01T00:00:00+00:00' "
                        "WHERE schedule_id=? AND slot_index=0",
                        (schedule.schedule_id,),
                    )
                connection.rollback()
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "DELETE FROM collector_frozen_schedules_v1 "
                        "WHERE schedule_id=?",
                        (schedule.schedule_id,),
                    )
            finally:
                connection.rollback()
                connection.close()

            reloaded = load_frozen_acquisition_schedule(
                store, schedule.schedule_id
            )
            self.assertEqual(reloaded.commitment_sha256, schedule.commitment_sha256)

    def test_product_truth_objects_are_not_caller_constructible(self):
        with self.assertRaises(TypeError):
            FrozenAcquisitionSchedule()
        with self.assertRaises(TypeError):
            ScheduledCycleCoverage()

    def test_schedule_commitment_is_independent_of_query_scope_dict_order(self):
        with tempfile.TemporaryDirectory() as left_tmp, tempfile.TemporaryDirectory() as right_tmp:
            left_store = CollectorDeltaStore(Path(left_tmp) / "collector.db")
            right_store = CollectorDeltaStore(Path(right_tmp) / "collector.db")
            left = _freeze(
                left_store,
                scope={"league": "a", "market": "winner"},
            )
            right = _freeze(
                right_store,
                scope={"market": "winner", "league": "a"},
            )
            self.assertEqual(left.schedule_id, right.schedule_id)
            self.assertEqual(left.commitment_sha256, right.commitment_sha256)

    def test_naive_product_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            with self.assertRaisesRegex(
                ScheduledCycleCoverageError, "timezone-aware"
            ):
                freeze_acquisition_schedule(
                    store,
                    source_id="source-x",
                    adapter_id="adapter-v1",
                    sport="table_tennis",
                    query_scope={"league": "a"},
                    schedule_policy_version="cadence-v1",
                    window_start="2026-01-01T00:00:10",
                    window_end="2026-01-01T00:00:40+00:00",
                    campaign_id="campaign-1",
                    protocol_id="protocol-1",
                    collector_config=_config(),
                    _clock=lambda: "2026-01-01T00:00:00+00:00",
                )


if __name__ == "__main__":
    unittest.main()
