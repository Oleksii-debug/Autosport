from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.collector_service import HeadlessCollectorService
from autosport.event_lifecycle import CatalogPage, ContinuousEventLifecycle


class _BlockingCatalogSource:
    source_id = "source-x"
    stream_epoch = "epoch-1"

    def __init__(self) -> None:
        self.entered_catalog = threading.Event()
        self.release_catalog = threading.Event()
        self.catalog_calls = 0
        self.delta_calls = 0

    def fetch_catalog_page(self, _checkpoint):
        self.catalog_calls += 1
        self.entered_catalog.set()
        if not self.release_catalog.wait(5):
            raise AssertionError("test cleanup failed to release blocked catalog call")
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch="catalog-epoch-1",
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, _checkpoint, _records, _max_items):
        self.delta_calls += 1
        return ()


class CollectorInflightCyclePostStopTests(unittest.TestCase):
    def test_durable_stop_fences_terminal_success_from_already_started_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _BlockingCatalogSource()
            service = HeadlessCollectorService(
                delta_store=CollectorDeltaStore(root / "collector.db"),
                lifecycle=ContinuousEventLifecycle(root / "catalog.json"),
                source=source,
                state_path=root / "service.json",
                run_id="run-post-stop-fence",
                clock=lambda: "2026-09-22T02:20:00+00:00",
            )

            results: list[object] = []
            errors: list[BaseException] = []

            def run_cycle() -> None:
                try:
                    results.append(service.run_cycle())
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(
                target=run_cycle,
                name="collector-cycle-blocked-inside-provider",
            )
            thread.start()
            self.assertTrue(
                source.entered_catalog.wait(1),
                "collector cycle never entered the blocking provider call",
            )

            service.stop("operator_stop")
            stopped_before_release = service.status()
            self.assertEqual(stopped_before_release["stop_reason"], "operator_stop")
            self.assertIsNotNone(stopped_before_release["stopped_at"])
            self.assertEqual(stopped_before_release["cycles_attempted"], 1)
            self.assertEqual(stopped_before_release["cycles_succeeded"], 0)

            source.release_catalog.set()
            thread.join(2)

            self.assertFalse(thread.is_alive())
            final_status = service.status()

            self.assertEqual(
                results,
                [],
                "an already-started collector cycle returned success after durable STOP",
            )
            self.assertEqual(
                final_status["cycles_succeeded"],
                0,
                "an already-started collector cycle recorded terminal success after STOP",
            )
            self.assertIsNone(
                final_status["last_success_at"],
                "post-STOP cycle completion must not become the last successful cycle",
            )
            self.assertEqual(final_status["stop_reason"], "operator_stop")
            self.assertEqual(
                final_status["stopped_at"],
                stopped_before_release["stopped_at"],
            )
            self.assertEqual(source.catalog_calls, 1)
            self.assertLessEqual(source.delta_calls, 1)

            # A repair may abort through the existing internal STOP control-flow
            # signal or another fail-closed stop-specific exception. The safety law
            # above is about forbidden post-STOP success, not one exception class.
            self.assertLessEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
