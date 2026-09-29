from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import autosport._collector_service_state_serialization as serialization
from autosport.causal_collector import CollectorDeltaStore
from autosport.collector_service import (
    CollectorServiceStoppedError,
    HeadlessCollectorService,
)
from autosport.event_lifecycle import CatalogPage, ContinuousEventLifecycle


class _EmptySource:
    source_id = "source-x"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, _checkpoint):
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch="catalog-epoch-1",
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, _checkpoint, _records, _max_items):
        return ()


class CollectorServiceSerializationDispatchSealTests(unittest.TestCase):
    def _service(self, root: Path, run_id: str) -> HeadlessCollectorService:
        return HeadlessCollectorService(
            delta_store=CollectorDeltaStore(root / f"{run_id}.db"),
            lifecycle=ContinuousEventLifecycle(root / f"{run_id}-catalog.json"),
            source=_EmptySource(),
            state_path=root / f"{run_id}-service.json",
            run_id=run_id,
            clock=lambda: "2026-09-29T04:45:00+00:00",
        )

    def test_installed_run_cycle_ignores_rebound_original_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp), "run-cycle-global")
            hostile_calls: list[str] = []
            original_global = serialization._ORIGINAL_RUN_CYCLE

            def hostile(_service):
                hostile_calls.append("run_cycle")
                raise AssertionError("rebound module global must not become authority")

            serialization._ORIGINAL_RUN_CYCLE = hostile
            try:
                result = service.run_cycle()
            finally:
                serialization._ORIGINAL_RUN_CYCLE = original_global

            self.assertEqual(result.source_id, "source-x")
            self.assertEqual(hostile_calls, [])
            self.assertEqual(service.status()["cycles_succeeded"], 1)

    def test_stop_and_post_stop_fence_ignore_rebound_globals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp), "state-global")
            hostile_calls: list[str] = []
            original_stop_global = serialization._ORIGINAL_STOP
            original_update_global = serialization._ORIGINAL_UPDATE
            original_fence_global = serialization._fence_post_stop_mutation

            def hostile_stop(_service, _reason="operator_stop"):
                hostile_calls.append("stop")
                raise AssertionError("rebound STOP global must not become authority")

            def hostile_update(_state, _mutate):
                hostile_calls.append("update")
                raise AssertionError("rebound update global must not become authority")

            serialization._ORIGINAL_STOP = hostile_stop
            serialization._ORIGINAL_UPDATE = hostile_update
            serialization._fence_post_stop_mutation = lambda mutate: mutate
            try:
                service.stop("operator_stop")
                stopped = service.status()
                self.assertEqual(stopped["stop_reason"], "operator_stop")
                with self.assertRaises(CollectorServiceStoppedError):
                    service._state.record_success(
                        at="2026-09-29T04:45:01+00:00",
                        committed=0,
                        duplicates=0,
                    )
            finally:
                serialization._ORIGINAL_STOP = original_stop_global
                serialization._ORIGINAL_UPDATE = original_update_global
                serialization._fence_post_stop_mutation = original_fence_global

            self.assertEqual(hostile_calls, [])
            self.assertEqual(service.status()["cycles_succeeded"], 0)

    def test_installed_wrappers_ignore_rebound_lock_and_depth_globals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp), "lock-global")
            hostile_calls: list[str] = []
            original_state_lock = serialization._CollectorServiceStateMutationLock
            original_cycle_lock = serialization._CollectorServiceCycleMutationLock
            original_depths = serialization._depths

            class HostileLock:
                def __init__(self, *_args, **_kwargs):
                    hostile_calls.append("lock")

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

            serialization._CollectorServiceStateMutationLock = HostileLock
            serialization._CollectorServiceCycleMutationLock = HostileLock
            serialization._depths = lambda: {}
            try:
                result = service.run_cycle()
                service.stop("operator_stop")
            finally:
                serialization._CollectorServiceStateMutationLock = original_state_lock
                serialization._CollectorServiceCycleMutationLock = original_cycle_lock
                serialization._depths = original_depths

            self.assertEqual(result.source_id, "source-x")
            self.assertEqual(hostile_calls, [])
            self.assertEqual(service.status()["cycles_succeeded"], 1)
            self.assertEqual(service.status()["stop_reason"], "operator_stop")


if __name__ == "__main__":
    unittest.main()
