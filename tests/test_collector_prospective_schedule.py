from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.collector_service import (
    CollectorServiceConfig,
    CollectorServiceError,
    HeadlessCollectorService,
)
from autosport.event_lifecycle import CatalogPage, ContinuousEventLifecycle


class _Clock:
    def __init__(self, value: str) -> None:
        self.current = datetime.fromisoformat(value.replace("Z", "+00:00"))
        self.sleeps: list[float] = []

    def __call__(self) -> str:
        return self.current.isoformat()

    def sleep(self, seconds: float) -> None:
        value = float(seconds)
        self.sleeps.append(value)
        self.current += timedelta(seconds=value)

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=float(seconds))


class _EmptySource:
    source_id = "source-x"
    stream_epoch = "epoch-1"

    def __init__(
        self,
        clock: _Clock,
        cycle_durations: list[float],
        *,
        start_position: int = 1,
        stream_epoch: str = "epoch-1",
    ) -> None:
        self.clock = clock
        self.cycle_durations = list(cycle_durations)
        self.position = start_position - 1
        self.stream_epoch = stream_epoch

    def fetch_catalog_page(self, checkpoint):
        self.position += 1
        if self.cycle_durations:
            self.clock.advance(self.cycle_durations.pop(0))
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch="catalog-epoch-1",
            cursor=f"catalog-{self.position}",
            position=self.position,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()


def _service(
    root: str,
    *,
    clock: _Clock,
    source: _EmptySource,
    interval_seconds: float = 10,
    evaluation_slot_count: int | None = None,
) -> HeadlessCollectorService:
    return HeadlessCollectorService(
        delta_store=CollectorDeltaStore(Path(root) / "collector.db"),
        lifecycle=ContinuousEventLifecycle(Path(root) / "catalog.json"),
        source=source,
        state_path=Path(root) / "service.json",
        run_id="run-1",
        config=CollectorServiceConfig(
            poll_interval_seconds=interval_seconds,
            evaluation_slot_count=evaluation_slot_count,
            retry_attempts=1,
            initial_backoff_seconds=1,
            max_backoff_seconds=1,
            jitter_fraction=0,
        ),
        clock=clock,
        sleep=clock.sleep,
        random_value=lambda: 0,
    )


class ProspectiveCollectorScheduleTests(unittest.TestCase):
    def test_run_uses_frozen_due_times_not_post_cycle_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _Clock("2026-01-01T00:00:00+00:00")
            service = _service(
                tmp,
                clock=clock,
                source=_EmptySource(clock, [7, 12, 0]),
            )

            result = service.run(max_cycles=3)

            self.assertEqual(result.cycles_executed, 3)
            # Cycle 1 consumes seven seconds, so cycle 2 waits only three.
            # Cycle 2 then overruns slot 2 by two seconds; no extra ten-second
            # completion-relative delay is inserted.
            self.assertEqual(clock.sleeps, [3.0])

            evidence = service.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=2,
            )
            self.assertEqual(
                tuple(slot["due_at"] for slot in evidence["slots"]),
                (
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:10+00:00",
                    "2026-01-01T00:00:20+00:00",
                ),
            )
            self.assertEqual(
                tuple(slot["attempted_at"] for slot in evidence["slots"]),
                (
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:10+00:00",
                    "2026-01-01T00:00:22+00:00",
                ),
            )
            self.assertEqual(
                tuple(slot["started_late"] for slot in evidence["slots"]),
                (False, False, True),
            )
            self.assertFalse(
                any(slot["started_before_due"] for slot in evidence["slots"])
            )
            self.assertEqual(
                tuple(slot["cycle_seq"] for slot in evidence["slots"]),
                (1, 2, 3),
            )
            self.assertEqual(len(evidence["commitment_sha256"]), 64)

    def test_restart_keeps_original_anchor_and_exposes_overdue_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_clock = _Clock("2026-01-01T00:00:00+00:00")
            first = _service(
                tmp,
                clock=first_clock,
                source=_EmptySource(first_clock, [0], start_position=1),
            )
            first.run(max_cycles=1)

            restart_clock = _Clock("2026-01-01T00:00:35+00:00")
            reopened = _service(
                tmp,
                clock=restart_clock,
                source=_EmptySource(restart_clock, [0], start_position=2),
            )
            reopened.resume()
            reopened.run(max_cycles=1)

            evidence = reopened.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(
                evidence["anchor_at"],
                "2026-01-01T00:00:00+00:00",
            )
            self.assertEqual(evidence["schema_version"], 4)
            self.assertEqual(evidence["stream_epoch"], "epoch-1")
            self.assertEqual(
                evidence["slots"][1]["due_at"],
                "2026-01-01T00:00:10+00:00",
            )
            self.assertEqual(
                evidence["slots"][1]["attempted_at"],
                "2026-01-01T00:00:35+00:00",
            )
            self.assertTrue(evidence["slots"][1]["started_late"])
            self.assertEqual(restart_clock.sleeps, [])

    def test_frozen_evaluation_window_survives_restart_while_max_cycles_is_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_clock = _Clock("2026-01-01T00:00:00+00:00")
            first = _service(
                tmp,
                clock=first_clock,
                source=_EmptySource(first_clock, [0], start_position=1),
                evaluation_slot_count=2,
            )
            first_result = first.run(max_cycles=1)
            self.assertEqual(first_result.cycles_executed, 1)

            before_restart = first.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(before_restart["evaluation_start_slot_ordinal"], 0)
            self.assertEqual(before_restart["evaluation_end_slot_ordinal"], 1)
            self.assertEqual(before_restart["bound_start_count"], 1)
            self.assertEqual(before_restart["missing_start_count"], 1)

            restart_clock = _Clock("2026-01-01T00:00:15+00:00")
            reopened = _service(
                tmp,
                clock=restart_clock,
                source=_EmptySource(restart_clock, [0], start_position=2),
                evaluation_slot_count=2,
            )
            reopened.resume()
            second_result = reopened.run(max_cycles=1)
            self.assertEqual(second_result.cycles_executed, 1)

            after_restart = reopened.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(after_restart["evaluation_start_slot_ordinal"], 0)
            self.assertEqual(after_restart["evaluation_end_slot_ordinal"], 1)
            self.assertEqual(after_restart["bound_start_count"], 2)
            self.assertEqual(after_restart["missing_start_count"], 0)
            self.assertEqual(
                tuple(slot["cycle_seq"] for slot in after_restart["slots"]),
                (1, 2),
            )

    def test_frozen_evaluation_window_cannot_be_rebound_after_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_clock = _Clock("2026-01-01T00:00:00+00:00")
            first = _service(
                tmp,
                clock=first_clock,
                source=_EmptySource(first_clock, [0], start_position=1),
                evaluation_slot_count=2,
            )
            first.run(max_cycles=1)

            restart_clock = _Clock("2026-01-01T00:00:15+00:00")
            reopened = _service(
                tmp,
                clock=restart_clock,
                source=_EmptySource(restart_clock, [0], start_position=2),
                evaluation_slot_count=1,
            )
            reopened.resume()
            with self.assertRaisesRegex(
                CollectorServiceError,
                "prospective collector schedule authority",
            ):
                reopened.run(max_cycles=1)

            evidence = reopened.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(evidence["evaluation_start_slot_ordinal"], 0)
            self.assertEqual(evidence["evaluation_end_slot_ordinal"], 1)
            self.assertEqual(evidence["bound_start_count"], 1)
            self.assertEqual(evidence["missing_start_count"], 1)

    def test_restart_rejects_stream_epoch_rebind_after_zero_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_clock = _Clock("2026-01-01T00:00:00+00:00")
            first = _service(
                tmp,
                clock=first_clock,
                source=_EmptySource(
                    first_clock,
                    [0],
                    start_position=1,
                    stream_epoch="epoch-1",
                ),
            )
            first.run(max_cycles=1)

            restart_clock = _Clock("2026-01-01T00:00:35+00:00")
            reopened = _service(
                tmp,
                clock=restart_clock,
                source=_EmptySource(
                    restart_clock,
                    [0],
                    start_position=2,
                    stream_epoch="epoch-2",
                ),
            )
            reopened.resume()

            with self.assertRaisesRegex(
                CollectorServiceError,
                "prospective collector schedule authority",
            ):
                reopened.run(max_cycles=1)

            evidence = reopened.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=0,
            )
            self.assertEqual(evidence["stream_epoch"], "epoch-1")
            self.assertEqual(evidence["bound_start_count"], 1)
            self.assertEqual(evidence["slots"][0]["stream_epoch"], "epoch-1")
            cycles = reopened.delta_store.collector_cycle_evidence(
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            self.assertEqual(len(cycles), 1)

    def test_schedule_interval_cannot_be_rebound_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_clock = _Clock("2026-01-01T00:00:00+00:00")
            first = _service(
                tmp,
                clock=first_clock,
                source=_EmptySource(first_clock, [0], start_position=1),
            )
            first.run(max_cycles=1)

            restart_clock = _Clock("2026-01-01T00:00:20+00:00")
            reopened = _service(
                tmp,
                clock=restart_clock,
                source=_EmptySource(restart_clock, [0], start_position=2),
                interval_seconds=20,
            )
            reopened.resume()
            with self.assertRaisesRegex(
                CollectorServiceError,
                "prospective collector schedule authority",
            ):
                reopened.run(max_cycles=1)

            evidence = reopened.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=0,
            )
            self.assertEqual(evidence["interval_seconds"], "10.0")
            self.assertEqual(restart_clock.sleeps, [])

    def test_unstarted_schedule_window_exposes_exact_missing_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _Clock("2026-01-01T00:00:00+00:00")
            service = _service(
                tmp,
                clock=clock,
                source=_EmptySource(clock, [0]),
            )
            service.run(max_cycles=1)

            evidence = service.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(evidence["expected_slot_count"], 2)
            self.assertEqual(evidence["bound_start_count"], 1)
            self.assertEqual(evidence["missing_start_count"], 1)
            self.assertEqual(evidence["early_start_count"], 0)
            self.assertEqual(evidence["late_start_count"], 0)
            self.assertEqual(evidence["slots"][0]["cycle_seq"], 1)
            self.assertEqual(
                evidence["slots"][1],
                {
                    "slot_ordinal": 1,
                    "due_at": "2026-01-01T00:00:10+00:00",
                    "cycle_seq": None,
                    "stream_epoch": None,
                    "attempted_at": None,
                    "started_before_due": None,
                    "started_late": None,
                },
            )
            self.assertEqual(len(evidence["commitment_sha256"]), 64)

    def test_caller_cannot_rebind_due_time_or_skip_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            store._ensure_collector_schedule(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                anchor_at="2026-01-01T00:00:00+00:00",
                interval_seconds=10,
            max_items=250,
            )
            slot = store._next_collector_schedule_slot(
                source_id="source-x",
                run_id="run-1",
            )

            with self.assertRaisesRegex(
                ValueError,
                "stream_epoch does not match current source",
            ):
                store._begin_scheduled_collector_cycle(
                    source_id="source-x",
                    run_id="run-1",
                    stream_epoch="epoch-2",
                    max_items=250,
                    slot_ordinal=slot["slot_ordinal"],
                    due_at=slot["due_at"],
                    attempted_at="2026-01-01T00:00:00+00:00",
                )
            self.assertEqual(
                store._next_collector_schedule_slot(
                    source_id="source-x",
                    run_id="run-1",
                ),
                slot,
            )

            with self.assertRaisesRegex(ValueError, "due_at is not canonical"):
                store._begin_scheduled_collector_cycle(
                    source_id="source-x",
                    run_id="run-1",
                    stream_epoch="epoch-1",
                    max_items=250,
                    slot_ordinal=0,
                    due_at="2026-01-01T00:00:01+00:00",
                    attempted_at="2026-01-01T00:00:01+00:00",
                )
            with self.assertRaisesRegex(
                ValueError,
                "duplicate, skipped, or out of order",
            ):
                store._begin_scheduled_collector_cycle(
                    source_id="source-x",
                    run_id="run-1",
                    stream_epoch="epoch-1",
                    max_items=250,
                    slot_ordinal=1,
                    due_at="2026-01-01T00:00:10+00:00",
                    attempted_at="2026-01-01T00:00:10+00:00",
                )

            self.assertEqual(
                store._next_collector_schedule_slot(
                    source_id="source-x",
                    run_id="run-1",
                ),
                slot,
            )

    def test_schedule_rows_are_sql_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            store._ensure_collector_schedule(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                anchor_at="2026-01-01T00:00:00+00:00",
                interval_seconds=10,
            max_items=250,
            )
            slot = store._next_collector_schedule_slot(
                source_id="source-x",
                run_id="run-1",
            )
            store._begin_scheduled_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                max_items=250,
                slot_ordinal=slot["slot_ordinal"],
                due_at=slot["due_at"],
                attempted_at="2026-01-01T00:00:00+00:00",
            )

            connection = sqlite3.connect(path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "UPDATE collector_schedules_v1 "
                        "SET interval_seconds='1.0' "
                        "WHERE source_id='source-x' AND run_id='run-1'"
                    )
                connection.rollback()
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "DELETE FROM collector_schedule_slots_v1 "
                        "WHERE source_id='source-x' AND run_id='run-1' "
                        "AND slot_ordinal=0"
                    )
            finally:
                connection.rollback()
                connection.close()

    def test_one_due_slot_cannot_mint_two_cycle_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            store._ensure_collector_schedule(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                anchor_at="2026-01-01T00:00:00+00:00",
                interval_seconds=10,
            max_items=250,
            )
            slot = store._next_collector_schedule_slot(
                source_id="source-x",
                run_id="run-1",
            )
            cycle_seq = store._begin_scheduled_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                max_items=250,
                slot_ordinal=slot["slot_ordinal"],
                due_at=slot["due_at"],
                attempted_at="2026-01-01T00:00:00+00:00",
            )
            self.assertEqual(cycle_seq, 1)

            with self.assertRaisesRegex(
                ValueError,
                "duplicate, skipped, or out of order",
            ):
                store._begin_scheduled_collector_cycle(
                    source_id="source-x",
                    run_id="run-1",
                    stream_epoch="epoch-1",
                    max_items=250,
                    slot_ordinal=slot["slot_ordinal"],
                    due_at=slot["due_at"],
                    attempted_at="2026-01-01T00:00:00+00:00",
                )

            cycles = store.collector_cycle_evidence(
                source_id="source-x",
                start_cycle_seq=1,
                end_cycle_seq=1,
            )
            self.assertEqual(len(cycles), 1)
            schedule = store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=0,
            )
            self.assertEqual(schedule["slots"][0]["cycle_seq"], 1)


if __name__ == "__main__":
    unittest.main()
