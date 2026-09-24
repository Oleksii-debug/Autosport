from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import ProductCompositionError, build_autonomous_product_runtime


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-09-21T17:20:00+00:00"

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
        raise AssertionError("no market delta should be resolved in this test")


class ProductRuntimeCallerLeaseReleaseFalsifierTests(unittest.TestCase):
    def test_caller_cannot_release_live_runtime_lease_and_keep_runtime_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            second = None
            try:
                # This is deliberately an adversarial caller action. The runtime-wide
                # lease is an authority capability, not an operator API. If a caller can
                # release it directly while the runtime remains open, a second canonical
                # runtime can enter the same workspace even though the first object still
                # considers itself authority-bearing.
                first._runtime_lease.release()

                second = build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "no longer owns workspace authority|workspace authority",
                ):
                    first.tick()
            finally:
                if second is not None:
                    second.close()
                first.close()


if __name__ == "__main__":
    unittest.main()
