"""Negative qualification contract tests. No fabricated machine or NVDA PASS."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/qualify_plan6_source.py"
spec = importlib.util.spec_from_file_location("plan6_qualifier", SCRIPT)
qualifier = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(qualifier)

class Plan6QualifierTests(unittest.TestCase):
    SHA = "a" * 40

    def _fake_root(self, root):
        for _, paths in qualifier.GROUPS:
            for path in paths:
                item = root / path
                item.parent.mkdir(parents=True, exist_ok=True)
                item.write_text("# fixture\n")

    def test_all_groups_source_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._fake_root(root)
            with patch.object(qualifier, "_identity", return_value=self.SHA):
                with patch.object(qualifier, "_test_group", side_effect=lambda r, n, p, timeout: {
                    "group": n, "status": "PASS", "passed": 3, "exit_code": 0}):
                    report = qualifier.run(root)
            self.assertEqual(report["status"], "PASS_SOURCE_ONLY")
            self.assertEqual(report["executed_pass_count"], 12)
            self.assertEqual(len(report["groups"]), 4)
            for flag in ("windows_packaged_candidate_verified", "complete_executable_sbom_verified",
                         "license_clearance_verified", "schema_migration_qualified", "human_tested",
                         "nvda_verified", "real_money_execution", "whole_product_complete"):
                self.assertIs(report[flag], False)

    def test_changed_head_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._fake_root(root)
            with patch.object(qualifier, "_identity", side_effect=[self.SHA, "b" * 40]):
                with patch.object(qualifier, "_test_group",
                                  return_value={"status": "PASS", "passed": 2,
                                                "exit_code": 0, "group": "x"}):
                    report = qualifier.run(root)
            self.assertEqual(report["status"], "FAIL_OR_INCOMPLETE")
            self.assertFalse(report["tracked_tree_stable"])

    def test_skipped_zero_tests_fails(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._fake_root(root)
            with patch.object(qualifier.subprocess, "run") as runner:
                runner.return_value.stdout = "8 skipped in 0.01s"
                runner.return_value.stderr = ""
                runner.return_value.returncode = 0
                name, paths = qualifier.GROUPS[0]
                result = qualifier._test_group(root, name, paths, timeout=3)
            self.assertEqual(result["status"], "FAIL")

    def test_missing_test_source_never_executes(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(qualifier.subprocess, "run") as runner:
                result = qualifier._test_group(
                    Path(d), "x", ("tests/not-present.py",), timeout=3)
                runner.assert_not_called()
            self.assertEqual(result["status"], "FAIL_MISSING_TEST_SOURCE")

    def test_nonzero_exit_never_passes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._fake_root(root)
            with patch.object(qualifier.subprocess, "run") as runner:
                runner.return_value.stdout = "3 passed, 1 failed in 1s"
                runner.return_value.stderr = ""
                runner.return_value.returncode = 1
                name, paths = qualifier.GROUPS[0]
                result = qualifier._test_group(root, name, paths, timeout=3)
            self.assertEqual(result["status"], "FAIL")

    def test_atomic_report_contains_actual_negative_truth(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "result.json"
            report = {"status": "FAIL_OR_INCOMPLETE", "nvda_verified": False}
            qualifier._write_report(path, report)
            self.assertEqual(json.loads(path.read_text()), report)
            self.assertEqual(list(Path(d).glob(".plan6-qual-*")), [])

if __name__ == "__main__":
    unittest.main()
