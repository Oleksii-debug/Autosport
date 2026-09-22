from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import build_autonomous_product_runtime


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-09-22T03:00:00+00:00"

    def __call__(self) -> str:
        return self.value


class _EmptyProductSource:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def __init__(self) -> None:
        self.catalog_calls = 0
        self.delta_calls = 0

    def fetch_catalog_page(self, _checkpoint):
        self.catalog_calls += 1
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor=f"catalog-{self.catalog_calls}",
            position=self.catalog_calls,
            events=(),
        )

    def fetch_deltas(self, _checkpoint, _records, _max_items):
        self.delta_calls += 1
        return ()

    def resolve_event(self, _delta):
        raise AssertionError("zero-result product tick must not resolve an event")


class ProductRuntimeProspectiveScheduleWiringTests(unittest.TestCase):
    def test_product_runtime_tick_is_bound_to_frozen_collector_due_slot(self) -> None:
        """The canonical product path must not create an unscheduled forward cycle.

        #1180 makes HeadlessCollectorService.run() prospective by freezing a schedule
        and binding each provider-facing cycle START to one due slot. The product
        runtime uses ContinuousSessionCoordinator.tick(), so the same source-universe
        claim is valid only if that path consumes the very same schedule authority.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _EmptyProductSource()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _seconds: None,
                initial_bankroll="100",
            )
            try:
                result = runtime.tick()

                self.assertEqual(result.cycle_index, 1)
                self.assertEqual(source.catalog_calls, 1)
                self.assertEqual(source.delta_calls, 1)

                # A positive product-runtime tick is provider-facing acquisition. It
                # must therefore have exactly the same frozen schedule evidence that
                # HeadlessCollectorService.run() provides, rather than merely an
                # otherwise-valid unscheduled collector_cycle START.
                schedule = runtime.collector.delta_store.collector_schedule_evidence(
                    source_id=source.source_id,
                    run_id=f"product:{source.source_id}",
                    start_slot_ordinal=0,
                    end_slot_ordinal=0,
                )
                self.assertEqual(schedule["schema_version"], 2)
                self.assertEqual(schedule["source_id"], source.source_id)
                self.assertEqual(schedule["run_id"], f"product:{source.source_id}")
                self.assertEqual(schedule["stream_epoch"], source.stream_epoch)
                self.assertEqual(schedule["expected_slot_count"], 1)
                self.assertEqual(schedule["bound_start_count"], 1)
                self.assertEqual(schedule["missing_start_count"], 0)
                self.assertEqual(schedule["early_start_count"], 0)

                slot = schedule["slots"][0]
                self.assertEqual(slot["slot_ordinal"], 0)
                self.assertEqual(slot["due_at"], clock.value)
                self.assertEqual(slot["attempted_at"], clock.value)
                self.assertEqual(slot["cycle_seq"], 1)
                self.assertFalse(slot["started_before_due"])

                cycles = runtime.collector.delta_store.collector_cycle_evidence(
                    source_id=source.source_id,
                    start_cycle_seq=1,
                    end_cycle_seq=1,
                )
                self.assertEqual(len(cycles), 1)
                self.assertEqual(cycles[0]["cycle_seq"], slot["cycle_seq"])
                self.assertEqual(cycles[0]["run_id"], f"product:{source.source_id}")
                self.assertEqual(cycles[0]["stream_epoch"], source.stream_epoch)
                self.assertEqual(cycles[0]["attempted_at"], slot["attempted_at"])
                self.assertEqual(cycles[0]["terminal"]["status"], "SUCCESS")
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
