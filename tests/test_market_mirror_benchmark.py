from __future__ import annotations

import importlib.util
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "benchmark_market_mirror.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "autosport_test_market_mirror_benchmark_script",
    _SCRIPT_PATH,
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load MarketMirror benchmark script")
_BENCHMARK = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BENCHMARK
_SPEC.loader.exec_module(_BENCHMARK)


class MarketMirrorBenchmarkTests(unittest.TestCase):
    def test_summary_reports_batch_and_per_operation_distributions(self) -> None:
        summary = _BENCHMARK._summary(
            [40, 10, 30, 20],
            operations_per_sample=2,
            outcome_counts=Counter({"applied": 8}),
        )

        self.assertEqual(summary["sample_count"], 4)
        self.assertEqual(summary["operations_per_sample"], 2)
        self.assertEqual(summary["total_operations"], 8)
        self.assertEqual(summary["batch_min_ns"], 10)
        self.assertEqual(summary["batch_median_ns"], 25)
        self.assertEqual(summary["batch_p95_ns"], 40)
        self.assertEqual(summary["batch_max_ns"], 40)
        self.assertEqual(summary["per_operation_min_ns"], 5)
        self.assertEqual(summary["per_operation_median_ns"], 12)
        self.assertEqual(summary["per_operation_p95_ns"], 20)
        self.assertEqual(summary["per_operation_max_ns"], 20)
        self.assertEqual(summary["samples_ns"], [40, 10, 30, 20])
        self.assertEqual(summary["outcome_counts"], {"applied": 8})

    def test_timed_rejects_timer_rollback(self) -> None:
        ticks = iter((20, 19))
        with self.assertRaisesRegex(RuntimeError, "timer moved backwards"):
            _BENCHMARK._timed(
                lambda: None,
                samples=1,
                operations_per_sample=1,
                timer_ns=lambda: next(ticks),
            )

    def test_run_benchmark_exercises_five_real_market_mirror_paths(self) -> None:
        report = _BENCHMARK.run_benchmark(
            source_sha="f" * 40,
            quote_count=4,
            samples=3,
            focused_key_count=2,
        )

        self.assertEqual(report["schema"], "autosport.market_mirror_benchmark")
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["source_sha"], "f" * 40)
        self.assertEqual(
            report["threshold_policy"],
            "measurement_only_no_pass_fail_threshold",
        )
        self.assertEqual(
            report["workload"],
            {
                "quote_count": 4,
                "samples": 3,
                "focused_key_count": 2,
                "sport": "table_tennis",
                "source_count": 1,
                "market_count": 1,
            },
        )

        scenarios = report["scenarios"]
        self.assertEqual(
            set(scenarios),
            {
                "distinct_insert_apply",
                "forward_update_apply",
                "exact_source_sport_lookup",
                "focused_active_projection",
                "full_snapshot_projection",
            },
        )
        for scenario in scenarios.values():
            self.assertEqual(scenario["sample_count"], 3)
            self.assertEqual(len(scenario["samples_ns"]), 3)
            self.assertGreaterEqual(scenario["batch_min_ns"], 0)
            self.assertGreaterEqual(
                scenario["batch_median_ns"],
                scenario["batch_min_ns"],
            )
            self.assertGreaterEqual(
                scenario["batch_p95_ns"],
                scenario["batch_median_ns"],
            )
            self.assertGreaterEqual(
                scenario["batch_max_ns"],
                scenario["batch_p95_ns"],
            )

        self.assertEqual(
            scenarios["distinct_insert_apply"]["outcome_counts"],
            {"applied": 12},
        )
        self.assertEqual(
            scenarios["forward_update_apply"]["outcome_counts"],
            {"applied": 12},
        )
        self.assertEqual(
            scenarios["exact_source_sport_lookup"]["outcome_counts"],
            {"found": 12},
        )
        self.assertEqual(
            scenarios["focused_active_projection"]["outcome_counts"],
            {"eligible": 6},
        )
        self.assertEqual(
            scenarios["full_snapshot_projection"]["outcome_counts"],
            {"snapshotted": 12},
        )
        self.assertEqual(
            scenarios["focused_active_projection"]["operations_per_sample"],
            1,
        )
        self.assertEqual(
            scenarios["full_snapshot_projection"]["operations_per_sample"],
            1,
        )

    def test_run_benchmark_rejects_invalid_workload_and_source_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            _BENCHMARK.run_benchmark(
                source_sha="a" * 40,
                quote_count=0,
            )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            _BENCHMARK.run_benchmark(
                source_sha="a" * 40,
                samples=0,
            )
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            _BENCHMARK.run_benchmark(
                source_sha="a" * 40,
                quote_count=2,
                focused_key_count=3,
            )
        with self.assertRaisesRegex(ValueError, "Git commit SHA"):
            _BENCHMARK.run_benchmark(source_sha="not-a-sha")

    def test_clean_source_sha_rejects_dirty_checkout(self) -> None:
        dirty = unittest.mock.Mock(stdout=" M scripts/benchmark_market_mirror.py\n")
        head = unittest.mock.Mock(stdout="a" * 40 + "\n")
        with patch(
            "subprocess.run",
            side_effect=(dirty, head),
        ):
            with self.assertRaisesRegex(RuntimeError, "clean checkout"):
                _BENCHMARK._clean_source_sha(Path("."))

    def test_script_has_no_network_or_sleep_dependency(self) -> None:
        source = _SCRIPT_PATH.read_text(encoding="utf-8")
        forbidden = (
            "import requests",
            "from requests",
            "import urllib",
            "from urllib",
            "import socket",
            "from socket",
            "time.sleep(",
        )
        for marker in forbidden:
            self.assertNotIn(marker, source)


if __name__ == "__main__":
    unittest.main()
