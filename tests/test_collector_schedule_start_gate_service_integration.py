from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.collector_service import CollectorServiceConfig, HeadlessCollectorService
from autosport.event_lifecycle import CatalogPage, ContinuousEventLifecycle


GATE = "a" * 64
AUTH = "b" * 64


class _Clock:
    def __init__(self) -> None:
        self.current = datetime.fromisoformat("2026-01-01T00:00:00+00:00")

    def __call__(self) -> str:
        return self.current.isoformat()

    def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=float(seconds))


class _CountingSource:
    source_id = "source-x"
    stream_epoch = "epoch-1"

    def __init__(self) -> None:
        self.catalog_calls = 0
        self.delta_calls = 0

    def fetch_catalog_page(self, _checkpoint):
        self.catalog_calls += 1
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch="catalog-epoch-1",
            cursor=f"catalog-{self.catalog_calls}",
            position=self.catalog_calls,
            events=(),
        )

    def fetch_deltas(self, _checkpoint, _records, _max_items):
        self.delta_calls += 1
        return ()


class CollectorScheduleStartGateServiceIntegrationTests(unittest.TestCase):
    def test_service_cannot_touch_provider_until_inception_gate_is_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = CollectorDeltaStore(root / "collector.db")
            schedule = store._ensure_collector_schedule(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                anchor_at="2026-01-01T00:00:00+00:00",
                interval_seconds=10,
                max_items=250,
                evaluation_start_slot_ordinal=0,
                evaluation_end_slot_ordinal=1,
                start_gate_binding_sha256=GATE,
            )
            source = _CountingSource()
            clock = _Clock()
            service = HeadlessCollectorService(
                delta_store=store,
                lifecycle=ContinuousEventLifecycle(root / "catalog.json"),
                source=source,
                state_path=root / "service.json",
                run_id="run-1",
                config=CollectorServiceConfig(
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

            with self.assertRaisesRegex(
                ValueError,
                "requires canonical scheduled START authority",
            ):
                service.run_cycle()
            with self.assertRaisesRegex(ValueError, "not durably authorized"):
                service.run(max_cycles=1)

            self.assertEqual(source.catalog_calls, 0)
            self.assertEqual(source.delta_calls, 0)
            self.assertEqual(
                store._next_collector_schedule_slot(
                    source_id="source-x",
                    run_id="run-1",
                )["slot_ordinal"],
                0,
            )

            store._authorize_collector_schedule_start_gate(
                source_id="source-x",
                run_id="run-1",
                schedule_id=schedule["schedule_id"],
                gate_binding_sha256=GATE,
                authorization_sha256=AUTH,
            )
            with self.assertRaisesRegex(
                ValueError,
                "requires canonical scheduled START authority",
            ):
                service.run_cycle()
            self.assertEqual(source.catalog_calls, 0)
            self.assertEqual(source.delta_calls, 0)

            result = service.run(max_cycles=1)

            self.assertEqual(result.cycles_executed, 1)
            self.assertEqual(source.catalog_calls, 1)
            self.assertEqual(source.delta_calls, 1)
            evidence = store.collector_schedule_evidence(
                source_id="source-x",
                run_id="run-1",
                start_slot_ordinal=0,
                end_slot_ordinal=0,
            )
            self.assertEqual(evidence["bound_start_count"], 1)
            self.assertEqual(evidence["slots"][0]["cycle_seq"], 1)


if __name__ == "__main__":
    unittest.main()
