from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.collector_service import (
    CollectorServiceStoppedError,
    HeadlessCollectorService,
)
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
    def test_external_stop_becomes_durable_only_after_inflight_cycle_quiesces(self) -> None:
        """No catalog/delta/error/status mutation may occur after durable STOP."""

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
            stop_started = threading.Event()
            stop_finished = threading.Event()

            def run_cycle() -> None:
                try:
                    results.append(service.run_cycle())
                except BaseException as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)

            def stop_service() -> None:
                stop_started.set()
                try:
                    service.stop("operator_stop")
                except BaseException as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)
                finally:
                    stop_finished.set()

            cycle_thread = threading.Thread(
                target=run_cycle,
                name="collector-cycle-blocked-inside-provider",
            )
            cycle_thread.start()
            self.assertTrue(
                source.entered_catalog.wait(1),
                "collector cycle never entered the blocking provider call",
            )

            stop_thread = threading.Thread(target=stop_service, name="collector-stop")
            stop_thread.start()
            self.assertTrue(stop_started.wait(1), "STOP thread did not start")

            # The cycle owns the path-shared mutation window. Releasing the provider
            # lets that exact pre-STOP transaction finish; only then may STOP become
            # durable. On the historical implementation STOP wins immediately here,
            # causing the terminal state fence to abort the older cycle instead.
            source.release_catalog.set()

            cycle_thread.join(2)
            stop_thread.join(2)
            self.assertFalse(cycle_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertTrue(stop_finished.is_set())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)

            final_status = service.status()
            self.assertEqual(final_status["cycles_attempted"], 1)
            self.assertEqual(final_status["cycles_succeeded"], 1)
            self.assertEqual(final_status["stop_reason"], "operator_stop")
            self.assertIsNotNone(final_status["stopped_at"])
            self.assertEqual(source.catalog_calls, 1)
            self.assertEqual(source.delta_calls, 1)

            # Durable STOP is now the post-quiescence boundary: after it is visible,
            # another cycle cannot publish any terminal/data mutation until resume.
            with self.assertRaises(CollectorServiceStoppedError):
                service.run_cycle()
            self.assertEqual(service.status(), final_status)


if __name__ == "__main__":
    unittest.main()
