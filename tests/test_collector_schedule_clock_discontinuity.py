from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.collector_service import CollectorServiceConfig, HeadlessCollectorService
from autosport.event_lifecycle import CatalogPage, ContinuousEventLifecycle


class _EarlyWakeClock:
    def __init__(self, value: str) -> None:
        self.current = datetime.fromisoformat(value.replace("Z", "+00:00"))
        self.sleeps: list[float] = []

    def __call__(self) -> str:
        return self.current.isoformat()

    def sleep(self, seconds: float) -> None:
        value = float(seconds)
        self.sleeps.append(value)
        if len(self.sleeps) == 1:
            self.current += timedelta(seconds=max(0.0, value - 2.0))
        else:
            self.current += timedelta(seconds=value)


class _EmptySource:
    source_id = "source-x"
    stream_epoch = "epoch-1"

    def __init__(self) -> None:
        self.position = 0

    def fetch_catalog_page(self, checkpoint):
        self.position += 1
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch="catalog-epoch-1",
            cursor=f"catalog-{self.position}",
            position=self.position,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()


class CollectorScheduleClockDiscontinuityTests(unittest.TestCase):
    def _store_with_schedule(self, root: str) -> CollectorDeltaStore:
        store = CollectorDeltaStore(Path(root) / "collector.db")
        store._ensure_collector_schedule(
            source_id="source-x",
            run_id="run-1",
            stream_epoch="epoch-1",
            anchor_at="2026-01-01T00:00:00+00:00",
            interval_seconds=10,
        max_items=250,
        )
        return store

    def test_service_rechecks_clock_after_premature_sleep_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = _EarlyWakeClock("2026-01-01T00:00:00+00:00")
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            service = HeadlessCollectorService(
                delta_store=store,
                lifecycle=ContinuousEventLifecycle(Path(tmp) / "catalog.json"),
                source=_EmptySource(),
                state_path=Path(tmp) / "service.json",
                run_id="run-1",
                config=CollectorServiceConfig(
                    poll_interval_seconds=10,
                    retry_attempts=1,
                    initial_backoff_seconds=1,
                    max_backoff_seconds=1,
                    jitter_fraction=0,
                ),
                clock=clock,
                sleep=clock.sleep,
                random_value=lambda: 0,
            )

            result = service.run(max_cycles=2)

            self.assertEqual(result.cycles_executed, 2)
            self.assertEqual(clock.sleeps, [10.0, 2.0])
            evidence = store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(
                evidence["slots"][1]["due_at"],
                "2026-01-01T00:00:10+00:00",
            )
            self.assertEqual(
                evidence["slots"][1]["attempted_at"],
                "2026-01-01T00:00:10+00:00",
            )
            self.assertFalse(evidence["slots"][1]["started_before_due"])

    def test_start_before_frozen_due_at_is_rejected_without_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_schedule(tmp)

            with self.assertRaisesRegex(ValueError, "precede frozen due_at"):
                store._begin_scheduled_collector_cycle(
                    source_id="source-x",
                    run_id="run-1",
                    stream_epoch="epoch-1",
                    max_items=250,
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
                max_items=250,
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
                    max_items=250,
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
