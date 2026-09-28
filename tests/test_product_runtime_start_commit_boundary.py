from __future__ import annotations

import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from autosport.continuous_session import SessionState
from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import build_autonomous_product_runtime


class Source:
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
        raise AssertionError("no market delta should be resolved")


def _crash_after_start_commit(workspace: str, ready) -> None:
    runtime = build_autonomous_product_runtime(
        workspace=Path(workspace),
        source=Source(),
        clock=lambda: "2026-09-22T14:12:00+00:00",
        sleep=lambda _: None,
        initial_bankroll="100",
    )
    runtime.stop("commit_boundary_precondition")

    original_mark_completed = runtime._start_transition_store.mark_completed

    def mark_completed_then_crash(generation: int) -> None:
        original_mark_completed(generation)
        ready.set()
        os._exit(91)

    runtime._start_transition_store.mark_completed = mark_completed_then_crash
    runtime.start()
    os._exit(99)


class ProductRuntimeStartCommitBoundaryTests(unittest.TestCase):
    def test_crash_after_completed_journal_restores_running_not_rollback(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            ready = context.Event()
            process = context.Process(
                target=_crash_after_start_commit,
                args=(directory, ready),
            )
            process.start()
            self.assertTrue(
                ready.wait(20),
                "child did not reach durable START commit boundary",
            )
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join(10)
                self.fail("child did not crash after durable START commit")
            self.assertEqual(process.exitcode, 91)

            restored = build_autonomous_product_runtime(
                workspace=Path(directory),
                source=Source(),
                clock=lambda: "2026-09-22T14:13:00+00:00",
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertEqual(restored.status().state, SessionState.RUNNING)
                self.assertIsNone(restored._start_transition_store.pending())
                self.assertIsNone(restored.collector.status()["stopped_at"])
                self.assertEqual(restored.start().state, SessionState.RUNNING)
            finally:
                try:
                    restored.stop("commit_boundary_cleanup")
                finally:
                    restored.close()


if __name__ == "__main__":
    unittest.main()
