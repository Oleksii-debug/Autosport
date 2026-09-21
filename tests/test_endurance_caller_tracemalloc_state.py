from __future__ import annotations

import tempfile
import tracemalloc
import unittest
from pathlib import Path

from autosport.endurance import EnduranceConfig, run_endurance


class EnduranceCallerTracemallocStateTests(unittest.TestCase):
    def test_run_preserves_caller_owned_historical_peak(self) -> None:
        if tracemalloc.is_tracing():
            self.skipTest("test requires ownership of the process tracemalloc session")

        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            prior_allocation = bytearray(16 * 1024 * 1024)
            del prior_allocation
            historical_peak = tracemalloc.get_traced_memory()[1]
            self.assertGreater(historical_peak, 8 * 1024 * 1024)

            config = EnduranceConfig(
                event_count=20,
                quote_keys=10,
                batch_size=10,
                restart_cycles=1,
                paper_tickets=5,
            )
            with tempfile.TemporaryDirectory() as tmp:
                report = run_endurance(Path(tmp), config)

            self.assertEqual(report.status, "PASS", report.failures)
            self.assertTrue(
                tracemalloc.is_tracing(),
                "run_endurance stopped a caller-owned tracemalloc session",
            )
            caller_peak_after = tracemalloc.get_traced_memory()[1]
            self.assertGreaterEqual(
                caller_peak_after,
                historical_peak,
                "run_endurance erased the caller-owned tracemalloc historical peak",
            )
        finally:
            if tracemalloc.is_tracing():
                tracemalloc.stop()


if __name__ == "__main__":
    unittest.main()
