from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.collector_service import CollectorServiceConfig
from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import build_autonomous_product_runtime


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-10-06T09:00:00+00:00"

    def __call__(self) -> str:
        return self.value


class _Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()

    def resolve_event(self, delta):
        raise AssertionError("empty provider cycle must not resolve a market delta")


class ProductRuntimeProspectiveScheduleCurrentMainTests(unittest.TestCase):
    def test_supported_runtime_binds_ticks_to_one_restart_safe_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            config = CollectorServiceConfig(evaluation_slot_count=2)

            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=clock,
                sleep=lambda _seconds: None,
                collector_config=config,
                initial_bankroll="100",
            )
            try:
                self.assertTrue(runtime.coordinator.prospective_collection)
                first = runtime.tick()
                self.assertEqual(first.cycle_index, 1)
                first_evidence = runtime.collector.delta_store.collector_schedule_evidence(
                    source_id="provider-a",
                    run_id="product:provider-a",
                    start_slot_ordinal=0,
                    end_slot_ordinal=1,
                )
                schedule_id = first_evidence["schedule_id"]
                anchor_at = first_evidence["anchor_at"]
                self.assertEqual(first_evidence["evaluation_start_slot_ordinal"], 0)
                self.assertEqual(first_evidence["evaluation_end_slot_ordinal"], 1)
                self.assertEqual(first_evidence["bound_start_count"], 1)
                self.assertEqual(first_evidence["missing_start_count"], 1)
            finally:
                runtime.close()

            clock.value = "2026-10-06T09:00:35+00:00"
            restored = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=clock,
                sleep=lambda _seconds: None,
                collector_config=CollectorServiceConfig(evaluation_slot_count=2),
                initial_bankroll="100",
            )
            try:
                self.assertTrue(restored.coordinator.prospective_collection)
                second = restored.tick()
                self.assertEqual(second.cycle_index, 2)
                evidence = restored.collector.delta_store.collector_schedule_evidence(
                    source_id="provider-a",
                    run_id="product:provider-a",
                    start_slot_ordinal=0,
                    end_slot_ordinal=1,
                )
                self.assertEqual(evidence["schedule_id"], schedule_id)
                self.assertEqual(evidence["anchor_at"], anchor_at)
                self.assertEqual(evidence["bound_start_count"], 2)
                self.assertEqual(evidence["missing_start_count"], 0)
                self.assertEqual(
                    tuple(slot["cycle_seq"] for slot in evidence["slots"]),
                    (1, 2),
                )
                self.assertTrue(evidence["slots"][1]["started_late"])
            finally:
                restored.close()

    def test_runtime_rejects_noncanonical_collector_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TypeError):
                build_autonomous_product_runtime(
                    workspace=Path(directory),
                    source=_Source(),
                    collector_config=object(),  # type: ignore[arg-type]
                    initial_bankroll="100",
                )


if __name__ == "__main__":
    unittest.main()
