from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import ProductCompositionError, build_autonomous_product_runtime


class _Clock:
    def __call__(self) -> str:
        return "2026-09-21T14:25:00+00:00"


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
        raise AssertionError("no market delta should be resolved in this test")


class ProductRuntimePostCloseLeaseTests(unittest.TestCase):
    def test_released_runtime_cannot_restart_after_new_owner_acquires_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            self.assertEqual(first.tick().cycle_index, 1)
            first.close()

            second = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertEqual(second.status().cycles_completed, 1)

                blocked_actions = (
                    ("start", first.start),
                    ("resume", first.resume),
                    ("pause", first.pause),
                    ("tick", first.tick),
                    ("status", first.status),
                    ("stop", lambda: first.stop("stale_owner_stop")),
                )
                for name, action in blocked_actions:
                    with self.subTest(action=name):
                        with self.assertRaisesRegex(
                            ProductCompositionError,
                            "closed|runtime lease|workspace authority",
                        ):
                            action()

                # The new owner remains the only runtime allowed to advance the product.
                self.assertEqual(second.tick().cycle_index, 2)
            finally:
                second.close()


if __name__ == "__main__":
    unittest.main()
