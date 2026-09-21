from __future__ import annotations

import ctypes
import gc
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.continuous_session import SessionState
from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import build_autonomous_product_runtime
from autosport.storage import SQLiteMarketStore


class _EmptySource:
    source_id = "process-resource-growth-source"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        position = 1 if checkpoint is None else int(checkpoint.position) + 1
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor=f"catalog-{position}",
            position=position,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()

    def resolve_event(self, delta):
        raise AssertionError("empty resource source must not resolve a market delta")


def _live_thread_count() -> int:
    return sum(1 for thread in threading.enumerate() if thread.is_alive())


def _process_handle_or_fd_count() -> int:
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetProcessHandleCount.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.GetProcessHandleCount.restype = ctypes.c_int

        count = ctypes.c_ulong()
        process = kernel32.GetCurrentProcess()
        if not kernel32.GetProcessHandleCount(process, ctypes.byref(count)):
            error = ctypes.get_last_error()
            raise OSError(error, "GetProcessHandleCount failed")
        return int(count.value)

    if sys.platform.startswith("linux"):
        return len(os.listdir("/proc/self/fd"))

    raise unittest.SkipTest(
        "process handle/fd count is qualified only on Windows and Linux"
    )


class ProductRuntimeProcessResourceGrowthTests(unittest.TestCase):
    def _baseline(self) -> tuple[int, int]:
        gc.collect()
        return _live_thread_count(), _process_handle_or_fd_count()

    def _assert_returns_to_baseline(
        self,
        *,
        baseline_threads: int,
        baseline_handles: int,
        cycle: str,
    ) -> None:
        gc.collect()
        self.assertEqual(
            _live_thread_count(),
            baseline_threads,
            f"{cycle}: live Python thread count grew after runtime close",
        )
        self.assertEqual(
            _process_handle_or_fd_count(),
            baseline_handles,
            f"{cycle}: process handle/fd count grew after runtime close",
        )

    def test_repeated_runtime_cycles_return_process_resources_to_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            baseline_threads, baseline_handles = self._baseline()
            session_id: str | None = None

            for expected_cycle in range(1, 13):
                runtime = build_autonomous_product_runtime(
                    workspace=workspace,
                    source=_EmptySource(),
                    initial_bankroll="100",
                )
                try:
                    started = runtime.start()
                    self.assertEqual(started.state, SessionState.RUNNING)
                    if session_id is None:
                        session_id = started.session_id
                    else:
                        self.assertEqual(started.session_id, session_id)

                    tick = runtime.tick()
                    self.assertEqual(tick.session_id, session_id)
                    self.assertEqual(tick.cycle_index, expected_cycle)

                    stopped = runtime.stop(
                        f"process_resource_growth_cycle_{expected_cycle}"
                    )
                    self.assertEqual(stopped.state, SessionState.STOPPED)
                    self.assertEqual(stopped.session_id, session_id)
                finally:
                    runtime.close()

                self._assert_returns_to_baseline(
                    baseline_threads=baseline_threads,
                    baseline_handles=baseline_handles,
                    cycle=f"closed cycle {expected_cycle}",
                )

    def test_failed_build_closes_open_market_store_resources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "product"
            baseline_threads, baseline_handles = self._baseline()

            with patch.object(
                SQLiteMarketStore,
                "current_by_source",
                side_effect=RuntimeError("forced current projection failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "forced current projection failure",
                ):
                    build_autonomous_product_runtime(
                        workspace=workspace,
                        source=_EmptySource(),
                        initial_bankroll="100",
                    )

            self._assert_returns_to_baseline(
                baseline_threads=baseline_threads,
                baseline_handles=baseline_handles,
                cycle="failed build cleanup",
            )


if __name__ == "__main__":
    unittest.main()
