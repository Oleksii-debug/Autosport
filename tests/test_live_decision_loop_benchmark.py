from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from autosport.live_decision_loop import LiveCycleResult, LiveCycleStatus


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "benchmark_live_decision_loop.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "autosport_test_live_decision_loop_benchmark_script",
    _SCRIPT_PATH,
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load live decision-loop benchmark script")
_BENCHMARK = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BENCHMARK
_SPEC.loader.exec_module(_BENCHMARK)


class LiveDecisionLoopBenchmarkTests(unittest.TestCase):
    def test_nearest_rank_and_summary_are_deterministic(self) -> None:
        self.assertEqual(_BENCHMARK._nearest_rank([50, 10, 30, 20, 40], 0.95), 50)
        results = [
            LiveCycleResult(LiveCycleStatus.NO_CHANGE),
            LiveCycleResult(LiveCycleStatus.NO_CHANGE),
            LiveCycleResult(LiveCycleStatus.BACKPRESSURE),
            LiveCycleResult(LiveCycleStatus.NO_CHANGE),
        ]
        summary = _BENCHMARK._summary([40, 10, 30, 20], results)
        self.assertEqual(summary["sample_count"], 4)
        self.assertEqual(summary["status_counts"], {"backpressure": 1, "no_change": 3})
        self.assertEqual(summary["min_ns"], 10)
        self.assertEqual(summary["median_ns"], 25)
        self.assertEqual(summary["p95_ns"], 40)
        self.assertEqual(summary["max_ns"], 40)
        self.assertEqual(summary["samples_ns"], [40, 10, 30, 20])

    def test_measure_rejects_timer_rollback(self) -> None:
        class _Loop:
            def run_cycle(self):
                return LiveCycleResult(LiveCycleStatus.NO_CHANGE)

        ticks = iter((20, 19))
        with self.assertRaisesRegex(RuntimeError, "timer moved backwards"):
            _BENCHMARK._measure(
                _Loop(),
                warmup_cycles=0,
                measured_cycles=1,
                timer_ns=lambda: next(ticks),
            )

    def test_run_benchmark_exercises_three_real_cycle_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = _BENCHMARK.run_benchmark(
                Path(directory),
                source_sha="f" * 40,
                warmup_cycles=1,
                measured_cycles=3,
                burst_size=3,
            )

        self.assertEqual(report["schema"], "autosport.live_decision_loop_benchmark")
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["source_sha"], "f" * 40)
        self.assertEqual(report["threshold_policy"], "measurement_only_no_pass_fail_threshold")
        self.assertEqual(
            report["workload"],
            {
                "warmup_cycles": 1,
                "measured_cycles": 3,
                "backpressure_burst_size": 3,
                "backpressure_max_dirty_per_cycle": 1,
            },
        )

        scenarios = report["scenarios"]
        self.assertEqual(
            set(scenarios),
            {"no_change", "single_dirty", "backpressure"},
        )
        for scenario in scenarios.values():
            self.assertEqual(scenario["sample_count"], 3)
            self.assertEqual(len(scenario["samples_ns"]), 3)
            self.assertEqual(sum(scenario["status_counts"].values()), 3)
            self.assertGreaterEqual(scenario["min_ns"], 0)
            self.assertGreaterEqual(scenario["median_ns"], scenario["min_ns"])
            self.assertGreaterEqual(scenario["p95_ns"], scenario["median_ns"])
            self.assertGreaterEqual(scenario["max_ns"], scenario["p95_ns"])

        self.assertEqual(scenarios["no_change"]["status_counts"], {"no_change": 3})
        self.assertEqual(
            scenarios["backpressure"]["status_counts"],
            {"backpressure": 3},
        )
        self.assertEqual(
            set(scenarios["single_dirty"]["status_counts"]),
            {"decided"},
        )

    def test_run_benchmark_rejects_invalid_workload_or_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "positive integer"):
                _BENCHMARK.run_benchmark(root, source_sha="a" * 40, measured_cycles=0)
            with self.assertRaisesRegex(ValueError, "Git commit SHA"):
                _BENCHMARK.run_benchmark(root, source_sha="not-a-sha")
            with self.assertRaisesRegex(ValueError, ">= 2"):
                _BENCHMARK.run_benchmark(
                    root,
                    source_sha="a" * 40,
                    measured_cycles=1,
                    burst_size=1,
                )


if __name__ == "__main__":
    unittest.main()
