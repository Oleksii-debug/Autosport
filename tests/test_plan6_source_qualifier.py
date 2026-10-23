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

    def test_untracked_test_hook_rejected(self):
        """An untracked conftest.py can alter pytest despite unchanged tracked HEAD."""
        def git_status(root, *args):
            if args[:2] == ("rev-parse", "--verify"):
                return self.SHA
            if args[:2] == ("status", "--porcelain"):
                return "?? tests/conftest.py" if "--untracked-files=all" in args else ""
            raise AssertionError(f"unexpected Git call: {args!r}")

        with patch.object(qualifier, "_git", side_effect=git_status):
            with self.assertRaisesRegex(ValueError, "untracked"):
                qualifier._identity(Path("."))

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

    def test_all_current_plan6_5b_sources_are_covered(self):
        """Future 5B releases cannot gain a source test outside exact-head qualification."""
        root = SCRIPT.parents[1]
        groups = dict(qualifier._source_groups(root))
        included = set(groups["release_integrity"])
        expected = {
            path.relative_to(root).as_posix()
            for path in (root / "tests").glob("test_plan6_5b_*.py")
        }
        self.assertTrue(expected, "Plan 6 5B tests must exist")
        self.assertFalse(expected - included, sorted(expected - included))
        self.assertIn("tests/test_plan6_source_qualifier.py", included)

    def test_new_5b_regression_is_discovered_exactly_once(self):
        """A concurrent 5B fix must not evade Plan-6 exact-source tests."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._fake_root(root)
            new_test = root / "tests/test_plan6_5b_license_record_capture.py"
            new_test.write_text("def test_license_record(): assert True\\n")
            groups = dict(qualifier._source_groups(root))
            paths = groups["release_integrity"]
            self.assertEqual(paths.count("tests/test_plan6_5b_license_record_capture.py"), 1)
            self.assertNotIn("tests/test_plan6_5b_license_record_capture.py",
                             dict(qualifier.GROUPS)["release_integrity"])
            with patch.object(qualifier, "_identity", return_value=self.SHA):
                with patch.object(qualifier, "_test_group", side_effect=lambda r, n, p, timeout: {
                    "group": n, "status": "PASS", "passed": 1, "exit_code": 0,
                }) as execute:
                    report = qualifier.run(root)
            self.assertEqual(report["status"], "PASS_SOURCE_ONLY")
            self.assertIn("tests/test_plan6_5b_license_record_capture.py",
                          execute.call_args_list[2].args[2])

    def test_unrelated_test_is_not_added_to_release_group(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._fake_root(root)
            extra = root / "tests/test_unrelated.py"
            extra.write_text("def test_unrelated(): assert True\\n")
            paths = dict(qualifier._source_groups(root))["release_integrity"]
            self.assertNotIn("tests/test_unrelated.py", paths)

    def test_current_release_path_integrity_sources_are_covered(self):
        """New release Windows/path regression modules must join exact-HEAD source gates."""
        root = SCRIPT.parents[1]
        covered = set(dict(qualifier.GROUPS)["release_integrity"])
        present = {
            path.relative_to(root).as_posix()
            for path in (root / "tests").glob("test_plan6_release_*.py")
        }
        self.assertIn("tests/test_release_package_source_link_policy_r124.py", covered)
        self.assertTrue(present, "Plan-6 Windows release path tests must exist")
        self.assertFalse(present - covered, sorted(present - covered))

    def test_current_install_update_recovery_sources_are_covered(self):
        """An added 6A or offline release negative test must enter the source gate."""
        root = SCRIPT.parents[1]
        covered = set(dict(qualifier.GROUPS)["paper_install_update_recovery"])
        present = {
            path.relative_to(root).as_posix()
            for pattern in ("test_offline_release_*.py", "test_plan6_6a_*.py",
                            "test_plan6_nested_release_*.py", "test_plan6_offline_install_*.py",
                            "test_plan6_update_alias_*.py")
            for path in (root / "tests").glob(pattern)
        }
        present.add("tests/test_data_tools_installed_release_entry.py")
        self.assertTrue(present)
        self.assertFalse(present - covered, sorted(present - covered))

    def test_atomic_report_contains_actual_negative_truth(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "result.json"
            report = {"status": "FAIL_OR_INCOMPLETE", "nvda_verified": False}
            qualifier._write_report(path, report)
            self.assertEqual(json.loads(path.read_text()), report)
            self.assertEqual(list(Path(d).glob(".plan6-qual-*")), [])

if __name__ == "__main__":
    unittest.main()
