from __future__ import annotations

import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from endurance_resource_probe import (  # noqa: E402
    ResourceLimit,
    ResourceSample,
    capture_resource_sample,
    qualify_resource_samples,
)
from autosport.storage import SQLiteMarketStore  # noqa: E402


class EnduranceResourceProbeTests(unittest.TestCase):
    @staticmethod
    def _sample(
        index: int,
        *,
        memory: int | None = 100,
        threads: int = 2,
        fds: int | None = 4,
        files: int = 1,
        workspace_bytes: int = 100,
    ) -> ResourceSample:
        return ResourceSample(
            checkpoint=f"checkpoint-{index}",
            work_units=index + 1,
            traced_memory_bytes=memory,
            thread_count=threads,
            open_fd_count=fds,
            workspace_file_count=files,
            workspace_bytes=workspace_bytes,
        )

    def test_no_declared_envelope_cannot_mint_pass(self):
        samples = [self._sample(index) for index in range(4)]

        result = qualify_resource_samples(samples, warmup_samples=1, limits={})

        self.assertEqual(result.status, "UNQUALIFIED")
        self.assertEqual(result.failures, ())
        self.assertTrue(any(item.signal == "thread_count" for item in result.trends))

    def test_stable_post_warmup_plateau_passes_declared_bounds(self):
        samples = [
            self._sample(0, memory=100, threads=2, fds=5),
            self._sample(1, memory=150, threads=2, fds=5),
            self._sample(2, memory=151, threads=2, fds=5),
            self._sample(3, memory=149, threads=2, fds=5),
            self._sample(4, memory=150, threads=2, fds=5),
        ]

        result = qualify_resource_samples(
            samples,
            warmup_samples=1,
            limits={
                "traced_memory_bytes": ResourceLimit(max_net_growth=4, max_span=4),
                "thread_count": ResourceLimit(max_net_growth=0, max_span=0),
                "open_fd_count": ResourceLimit(max_net_growth=0, max_span=0),
            },
        )

        self.assertEqual(result.status, "PASS", result.failures)
        memory = next(item for item in result.trends if item.signal == "traced_memory_bytes")
        self.assertEqual(memory.net_growth, 0)
        self.assertEqual(memory.span, 2)
        self.assertEqual(memory.slope_per_work_unit, "0")

    def test_monotonic_growth_fails_explicit_memory_envelope(self):
        samples = [
            self._sample(0, memory=100),
            self._sample(1, memory=140),
            self._sample(2, memory=180),
            self._sample(3, memory=220),
        ]

        result = qualify_resource_samples(
            samples,
            warmup_samples=0,
            limits={"traced_memory_bytes": ResourceLimit(max_net_growth=40, max_span=60)},
        )

        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("traced_memory_bytes net growth 120" in item for item in result.failures))
        self.assertTrue(any("traced_memory_bytes span 120" in item for item in result.failures))
        memory = next(item for item in result.trends if item.signal == "traced_memory_bytes")
        self.assertTrue(memory.monotonic_nondecreasing)
        self.assertEqual(memory.max_positive_step, 40)
        self.assertEqual(memory.slope_per_work_unit, "40")

    def test_required_platform_signal_fails_closed_when_unavailable(self):
        samples = [
            self._sample(0, fds=None),
            self._sample(1, fds=None),
            self._sample(2, fds=None),
        ]

        result = qualify_resource_samples(
            samples,
            warmup_samples=0,
            limits={"open_fd_count": ResourceLimit(max_net_growth=0, max_span=0)},
        )

        self.assertEqual(result.status, "FAIL")
        self.assertIn("open_fd_count", result.unsupported_signals)
        self.assertIn(
            "required signal open_fd_count is unavailable on this platform",
            result.failures,
        )

    def test_work_units_and_checkpoints_are_strictly_ordered(self):
        duplicate_checkpoint = [self._sample(0), self._sample(0)]
        with self.assertRaisesRegex(ValueError, "checkpoints must be unique"):
            qualify_resource_samples(
                duplicate_checkpoint,
                warmup_samples=0,
                limits={"thread_count": ResourceLimit(0, 0)},
            )

        non_advancing = [
            self._sample(0),
            ResourceSample(
                checkpoint="later",
                work_units=1,
                traced_memory_bytes=100,
                thread_count=2,
                open_fd_count=4,
                workspace_file_count=1,
                workspace_bytes=100,
            ),
        ]
        with self.assertRaisesRegex(ValueError, "strictly increasing work_units"):
            qualify_resource_samples(
                non_advancing,
                warmup_samples=0,
                limits={"thread_count": ResourceLimit(0, 0)},
            )

    def test_sqlite_reopen_close_cycles_do_not_accumulate_threads_or_descriptors(self):
        """Falsifier #16: repeated canonical store reopen/close must retire process resources."""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            was_tracing = tracemalloc.is_tracing()
            if not was_tracing:
                tracemalloc.start()
            try:
                samples: list[ResourceSample] = []
                for cycle in range(12):
                    store = SQLiteMarketStore(workspace / "market.db")
                    store.current()
                    store.close()
                    samples.append(
                        capture_resource_sample(
                            checkpoint=f"sqlite-closed-{cycle + 1}",
                            work_units=cycle + 1,
                            workspace=workspace,
                        )
                    )

                limits = {
                    "thread_count": ResourceLimit(max_net_growth=0, max_span=0),
                    "workspace_file_count": ResourceLimit(max_net_growth=0, max_span=0),
                }
                if all(sample.open_fd_count is not None for sample in samples):
                    limits["open_fd_count"] = ResourceLimit(max_net_growth=1, max_span=1)

                result = qualify_resource_samples(samples, warmup_samples=2, limits=limits)
            finally:
                if not was_tracing and tracemalloc.is_tracing():
                    tracemalloc.stop()

        self.assertEqual(result.status, "PASS", result.failures)
        thread_trend = next(item for item in result.trends if item.signal == "thread_count")
        self.assertEqual(thread_trend.net_growth, 0)
        self.assertEqual(thread_trend.span, 0)
        if "open_fd_count" not in result.unsupported_signals:
            fd_trend = next(item for item in result.trends if item.signal == "open_fd_count")
            self.assertLessEqual(fd_trend.net_growth, 1)
            self.assertLessEqual(fd_trend.span, 1)


if __name__ == "__main__":
    unittest.main()
