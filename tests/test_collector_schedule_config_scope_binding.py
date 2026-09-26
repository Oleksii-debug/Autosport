from __future__ import annotations

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


class _RecordingEmptySource:
    source_id = "source-x"
    stream_epoch = "epoch-1"

    def __init__(self, *, start_position: int) -> None:
        self.position = start_position - 1
        self.catalog_calls = 0
        self.seen_max_items: list[int] = []

    def fetch_catalog_page(self, checkpoint):
        self.catalog_calls += 1
        self.position += 1
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch="catalog-epoch-1",
            cursor=f"catalog-{self.position}",
            position=self.position,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        self.seen_max_items.append(max_items)
        return ()


def _service(
    root: str,
    *,
    clock: _Clock,
    source: _RecordingEmptySource,
    max_items: int,
) -> HeadlessCollectorService:
    return HeadlessCollectorService(
        delta_store=CollectorDeltaStore(Path(root) / "collector.db"),
        lifecycle=ContinuousEventLifecycle(Path(root) / "catalog.json"),
        source=source,
        state_path=Path(root) / "service.json",
        run_id="run-1",
        config=CollectorServiceConfig(
            max_items=max_items,
            poll_interval_seconds=10,
            evaluation_slot_count=2,
            retry_attempts=1,
            initial_backoff_seconds=1,
            max_backoff_seconds=1,
            jitter_fraction=0,
        ),
        clock=clock,
        sleep=clock.sleep,
        random_value=lambda: 0,
    )


class CollectorScheduleConfigScopeBindingTests(unittest.TestCase):
    def test_restart_with_same_acquisition_config_preserves_frozen_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_clock = _Clock("2026-01-01T00:00:00+00:00")
            first_source = _RecordingEmptySource(start_position=1)
            first = _service(
                tmp,
                clock=first_clock,
                source=first_source,
                max_items=1,
            )
            first.run(max_cycles=1)
            self.assertEqual(first_source.seen_max_items, [1])

            before = first.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(before["schema_version"], 4)
            self.assertEqual(before["max_items"], 1)

            restart_clock = _Clock("2026-01-01T00:00:15+00:00")
            restart_source = _RecordingEmptySource(start_position=2)
            reopened = _service(
                tmp,
                clock=restart_clock,
                source=restart_source,
                max_items=1,
            )
            reopened.resume()
            result = reopened.run(max_cycles=1)

            self.assertEqual(result.cycles_executed, 1)
            self.assertEqual(restart_source.seen_max_items, [1])
            after = reopened.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(after["schedule_id"], before["schedule_id"])
            self.assertEqual(after["max_items"], 1)
            self.assertEqual(after["bound_start_count"], 2)
            self.assertEqual(after["missing_start_count"], 0)

    def test_restart_cannot_rebind_max_items_under_same_frozen_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_clock = _Clock("2026-01-01T00:00:00+00:00")
            first_source = _RecordingEmptySource(start_position=1)
            first = _service(
                tmp,
                clock=first_clock,
                source=first_source,
                max_items=1,
            )
            first.run(max_cycles=1)

            before = first.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(before["max_items"], 1)
            self.assertEqual(before["bound_start_count"], 1)
            self.assertEqual(before["missing_start_count"], 1)

            restart_clock = _Clock("2026-01-01T00:00:15+00:00")
            restart_source = _RecordingEmptySource(start_position=2)
            reopened = _service(
                tmp,
                clock=restart_clock,
                source=restart_source,
                max_items=2,
            )
            reopened.resume()

            with self.assertRaisesRegex(
                CollectorServiceError,
                "prospective collector schedule authority",
            ):
                reopened.run(max_cycles=1)

            self.assertEqual(restart_source.seen_max_items, [])
            after = reopened.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(after["schedule_id"], before["schedule_id"])
            self.assertEqual(after["max_items"], 1)
            self.assertEqual(after["bound_start_count"], 1)
            self.assertEqual(after["missing_start_count"], 1)


    def test_forged_private_schedule_slot_cannot_override_durable_max_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first_clock = _Clock("2026-01-01T00:00:00+00:00")
            first_source = _RecordingEmptySource(start_position=1)
            first = _service(
                tmp,
                clock=first_clock,
                source=first_source,
                max_items=1,
            )
            first.run(max_cycles=1)

            before = first.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(before["max_items"], 1)
            self.assertEqual(before["missing_start_count"], 1)

            restart_clock = _Clock("2026-01-01T00:00:15+00:00")
            restart_source = _RecordingEmptySource(start_position=2)
            reopened = _service(
                tmp,
                clock=restart_clock,
                source=restart_source,
                max_items=2,
            )
            reopened.resume()
            frozen_slot = reopened.delta_store._next_collector_schedule_slot(
                source_id="source-x",
                run_id="run-1",
            )
            self.assertEqual(frozen_slot["max_items"], 1)
            forged_slot = dict(frozen_slot)
            forged_slot["max_items"] = 2

            with self.assertRaisesRegex(
                CollectorServiceError,
                "canonical due slot",
            ):
                reopened.run_cycle(_schedule_slot=forged_slot)

            self.assertEqual(restart_source.catalog_calls, 0)
            self.assertEqual(restart_source.seen_max_items, [])
            after = reopened.delta_store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=1,
            )
            self.assertEqual(after["schedule_id"], before["schedule_id"])
            self.assertEqual(after["max_items"], 1)
            self.assertEqual(after["bound_start_count"], 1)
            self.assertEqual(after["missing_start_count"], 1)



if __name__ == "__main__":
    unittest.main()
