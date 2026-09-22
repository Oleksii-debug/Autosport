from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from endurance_resource_probe import (  # noqa: E402
    ResourceLimit,
    ResourceSample,
    capture_resource_sample,
    qualify_resource_samples,
)
from autosport.endurance import EnduranceConfig, run_endurance  # noqa: E402


class EnduranceResourceProbeIntegrationTests(unittest.TestCase):
    def test_repeated_endurance_runs_release_closed_workspace_resources(self):
        config = EnduranceConfig(
            event_count=20,
            quote_keys=10,
            batch_size=10,
            restart_cycles=1,
            paper_tickets=5,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            samples: list[ResourceSample] = []
            for cycle in range(6):
                workspace = root / f"run-{cycle + 1}"
                report = run_endurance(workspace, config)
                self.assertEqual(report.status, "PASS", report.failures)

                moved = root / f"run-{cycle + 1}-closed-probe"
                workspace.rename(moved)
                moved.rename(workspace)

                samples.append(
                    capture_resource_sample(
                        checkpoint=f"endurance-closed-{cycle + 1}",
                        work_units=cycle + 1,
                        workspace=workspace,
                    )
                )

            limits = {
                "workspace_file_count": ResourceLimit(
                    max_net_growth=0,
                    max_span=0,
                    rationale=(
                        "equal deterministic endurance workloads must leave the same closed-workspace "
                        "file inventory after warmup"
                    ),
                )
            }
            if all(sample.open_fd_count is not None for sample in samples):
                limits["open_fd_count"] = ResourceLimit(
                    max_net_growth=0,
                    max_span=1,
                    rationale=(
                        "the /proc probe may observe one descriptor of measurement jitter, "
                        "but repeated closed endurance runs must return to the post-warmup baseline"
                    ),
                )

            result = qualify_resource_samples(samples, warmup_samples=1, limits=limits)

        self.assertEqual(result.status, "PASS", result.failures)
        self.assertIn("thread_count", result.unbounded_observed_signals)
        self.assertIn("traced_memory_bytes", result.unsupported_signals)
        file_trend = next(item for item in result.trends if item.signal == "workspace_file_count")
        self.assertEqual(file_trend.net_growth, 0)
        self.assertEqual(file_trend.span, 0)
        if "open_fd_count" not in result.unsupported_signals:
            fd_trend = next(item for item in result.trends if item.signal == "open_fd_count")
            self.assertEqual(fd_trend.net_growth, 0)
            self.assertLessEqual(fd_trend.span, 1)


if __name__ == "__main__":
    unittest.main()
