from __future__ import annotations

import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import ProductCompositionError, build_autonomous_product_runtime


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-09-21T08:45:00+00:00"

    def __call__(self) -> str:
        return self.value


class _Source:
    def __init__(self, source_id: str = "provider-a") -> None:
        self.source_id = source_id
        self.stream_epoch = "epoch-1"

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


def _hold_runtime_until_abrupt_exit(workspace: str, ready, crash) -> None:
    runtime = build_autonomous_product_runtime(
        workspace=Path(workspace),
        source=_Source(),
        clock=_Clock(),
        sleep=lambda _: None,
        initial_bankroll="100",
    )
    ready.set()
    if not crash.wait(20):
        os._exit(74)
    # Deliberately bypass runtime.close() and Python teardown. The product lease
    # must rely on OS handle lifetime so an actual process death releases authority.
    os._exit(73)


class ProductRuntimeProcessLeaseTests(unittest.TestCase):
    def test_same_workspace_rejects_second_runtime_until_first_closes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "already owns this workspace",
                ):
                    build_autonomous_product_runtime(
                        workspace=root,
                        source=_Source(),
                        clock=_Clock(),
                        sleep=lambda _: None,
                        initial_bankroll="100",
                    )

                # The runtime-wide lease is distinct from the existing economic lock:
                # a canonical tick must still be able to enter economic critical
                # sections without self-deadlocking.
                result = first.tick()
                self.assertEqual(result.cycle_index, 1)
            finally:
                first.close()

            restored = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertEqual(restored.status().cycles_completed, 1)
            finally:
                restored.close()

    def test_abrupt_owner_process_exit_releases_product_runtime_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            crash = context.Event()
            process = context.Process(
                target=_hold_runtime_until_abrupt_exit,
                args=(str(root), ready, crash),
            )
            process.start()
            self.assertTrue(ready.wait(20), "child product runtime did not acquire lease")
            try:
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "already owns this workspace",
                ):
                    build_autonomous_product_runtime(
                        workspace=root,
                        source=_Source(),
                        clock=_Clock(),
                        sleep=lambda _: None,
                        initial_bankroll="100",
                    )

                crash.set()
                process.join(20)
                if process.is_alive():
                    process.terminate()
                    process.join(10)
                    self.fail("child product runtime did not terminate after crash request")
                self.assertEqual(process.exitcode, 73)

                recovered = build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                recovered.close()
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(10)

    def test_failed_construction_releases_runtime_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = build_autonomous_product_runtime(
                workspace=root,
                source=_Source("provider-a"),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            seed.close()

            with self.assertRaisesRegex(
                ProductCompositionError,
                "source_id conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source("provider-b"),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

            # The manifest failure happened after lease acquisition. A fresh canonical
            # build must prove that exception unwinding released the process authority.
            recovered = build_autonomous_product_runtime(
                workspace=root,
                source=_Source("provider-a"),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            recovered.close()


if __name__ == "__main__":
    unittest.main()
