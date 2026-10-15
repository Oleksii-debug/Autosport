from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import plan5_qa_matrix as qa

GOOD_SHA = "a" * 40


class Plan5MatrixTests(unittest.TestCase):
    def test_roster_is_cross_authority_and_has_no_external_effects(self) -> None:
        self.assertEqual(
            set(qa.AREAS), {"causal", "financial", "provider", "recovery", "agent", "security"}
        )
        for paths in qa.AREAS.values():
            self.assertTrue(paths)
            self.assertTrue(all(p.startswith("tests/test_") and p.endswith(".py") for p in paths))

    def test_reject_malformed_source_identity(self) -> None:
        for value in ("", "A" * 40, "a" * 39, "a" * 40 + "x", "../" + GOOD_SHA):
            with self.subTest(value=value), self.assertRaises(qa.MatrixError):
                qa._sha(value)
        self.assertEqual(qa._sha(GOOD_SHA), GOOD_SHA)

    def _fixtures(self, root: Path) -> None:
        for paths in qa.AREAS.values():
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# deterministic QA fixture\n", encoding="utf-8")

    def test_preflight_and_deterministic_evidence_before_any_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixtures(root)
            with patch.object(qa, "_assert_checkout") as checkout, patch.object(
                qa, "_run_group", return_value=True
            ) as group:
                first = qa.run_matrix(root, GOOD_SHA)
                second = qa.run_matrix(root, GOOD_SHA)
            self.assertEqual(first, second)
            self.assertEqual(first["status"], "PASS")
            self.assertEqual(first["source_sha"], GOOD_SHA)
            self.assertEqual(len(first["areas"]), 6)
            self.assertFalse(first["real_money_execution"])
            self.assertFalse(first["human_tested"])
            self.assertFalse(first["nvda_verified"])
            self.assertEqual(group.call_count, 12)
            self.assertEqual(checkout.call_count, 2)

    def test_missing_fixture_fails_before_running_any_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixtures(root)
            (root / next(iter(qa.AREAS["security"]))).unlink()
            with patch.object(qa, "_assert_checkout"), patch.object(
                qa, "_run_group"
            ) as group:
                with self.assertRaises(qa.MatrixError):
                    qa.run_matrix(root, GOOD_SHA)
                group.assert_not_called()

    def test_failed_group_never_becomes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixtures(root)
            def qualify(_root: Path, paths: tuple[str, ...]) -> bool:
                return paths != qa.AREAS["provider"]
            with patch.object(qa, "_assert_checkout"), patch.object(
                qa, "_run_group", side_effect=qualify
            ):
                evidence = qa.run_matrix(root, GOOD_SHA)
            self.assertEqual(evidence["status"], "FAIL")
            self.assertEqual(evidence["areas"]["provider"], "FAIL")
            self.assertTrue(all(
                evidence["areas"][area] == "PASS" for area in qa.AREAS if area != "provider"
            ))

    def test_stale_report_is_invalidated_on_bad_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / "report.json"
            report.write_text('{"status":"PASS"}', encoding="utf-8")
            with patch.object(qa, "run_matrix", side_effect=qa.MatrixError("secret input")):
                result = qa.main([
                    "--source-sha", "invalid", "--repo-root", str(root),
                    "--output", str(report),
                ])
            self.assertEqual(result, 3)
            self.assertFalse(report.exists())

    def test_atomic_evidence_is_bounded_jsonl_and_no_secret_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "evidence.json"
            payload = {"status": "FAIL", "source_sha": GOOD_SHA}
            qa._publish(output, payload)
            raw = output.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertEqual(json.loads(raw), payload)
            self.assertEqual(list(Path(tmp).iterdir()), [output])

    def test_untrusted_test_path_and_oversize_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("../../.env", "missing", "tests/../secret.txt"):
                with self.subTest(rel=rel), self.assertRaises(qa.MatrixError):
                    qa._read_fixture(root, rel)
            path = root / "tests" / "test_size.py"
            path.parent.mkdir()
            path.write_bytes(b"x" * (qa.MAX_TEST_FILE_BYTES + 1))
            with self.assertRaises(qa.MatrixError):
                qa._read_fixture(root, "tests/test_size.py")

    def test_source_checkout_mismatch_fails(self) -> None:
        from subprocess import CompletedProcess
        with patch.object(
            qa.subprocess, "run",
            return_value=CompletedProcess(["git"], 0, stdout="b" * 40 + "\n"),
        ):
            with self.assertRaises(qa.MatrixError):
                qa._assert_checkout(Path("."), GOOD_SHA)


if __name__ == "__main__":
    unittest.main()
