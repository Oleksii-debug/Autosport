import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.release_package import build_windows_package, sha256_file, verify_windows_package
from autosport.run_registry import RepeatedExperimentError, RunRegistry, UnresolvedExperimentError
from autosport.session import AutosportSession


class RunRegistryReleaseTests(unittest.TestCase):
    def test_completed_dataset_strategy_cannot_repeat_silently(self):
        dataset = load_dataset(Path("examples/tt_demo"))
        with tempfile.TemporaryDirectory() as tmp:
            session = AutosportSession(tmp, "10000", strategy_id="baseline-v1")
            session.run_dataset(dataset)
            with self.assertRaises(RepeatedExperimentError):
                session.run_dataset(dataset)
            session.close()

    def test_unresolved_run_blocks_replay_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry(Path(tmp) / "registry.json")
            registry.begin("a" * 64, "b" * 64, "strategy", "run-1")
            with self.assertRaises(UnresolvedExperimentError):
                registry.begin("a" * 64, "b" * 64, "strategy", "run-2")

    def test_deterministic_package_ignores_source_mtimes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe, start, diagnostic, accessibility, example = self._package_inputs(root)
            first = root / "first.zip"
            second = root / "second.zip"
            _, first_hash = build_windows_package(
                exe, start, example, diagnostic, accessibility, first, "c" * 40
            )
            time.sleep(0.01)
            start.touch()
            _, second_hash = build_windows_package(
                exe, start, example, diagnostic, accessibility, second, "c" * 40
            )
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(sha256_file(first), sha256_file(second))

    def test_package_verifier_binds_source_hashes_truth_and_accessibility_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe, start, diagnostic, accessibility, example = self._package_inputs(root)
            package = root / "candidate.zip"
            source_sha = "d" * 40
            build_windows_package(
                exe, start, example, diagnostic, accessibility, package, source_sha
            )
            report = verify_windows_package(package, expected_source_sha=source_sha)
            self.assertEqual(report["status"], "PASS")
            self.assertFalse(report["real_money_execution"])
            self.assertFalse(report["human_tested"])
            self.assertFalse(report["nvda_verified"])
            with zipfile.ZipFile(package, "r") as archive:
                names = set(archive.namelist())
            self.assertIn("Autosport-V1/accessibility-audit.json", names)
            self.assertIn("Autosport-V1/packaged-diagnostic.json", names)
            with self.assertRaisesRegex(ValueError, "source_sha"):
                verify_windows_package(package, expected_source_sha="e" * 40)

    def test_package_verifier_rejects_machine_evidence_that_claims_nvda(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe, start, diagnostic, accessibility, example = self._package_inputs(root)
            accessibility.write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "real_money_execution": False,
                        "human_tested": False,
                        "nvda_verified": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            package = root / "candidate.zip"
            build_windows_package(
                exe, start, example, diagnostic, accessibility, package, "f" * 40
            )
            with self.assertRaisesRegex(ValueError, "nvda_verified=false"):
                verify_windows_package(package, expected_source_sha="f" * 40)

    @staticmethod
    def _package_inputs(root: Path) -> tuple[Path, Path, Path, Path, Path]:
        exe = root / "Autosport.exe"
        start = root / "WINDOWS_START_HERE.txt"
        diagnostic = root / "diag.json"
        accessibility = root / "a11y.json"
        example = root / "example"
        example.mkdir()
        exe.write_bytes(b"fake-exe-for-package-test")
        start.write_text("start\n", encoding="utf-8")
        evidence = {
            "status": "PASS",
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        diagnostic.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
        accessibility.write_text(json.dumps(evidence) + "\n", encoding="utf-8")
        (example / "data.jsonl").write_text("{}\n", encoding="utf-8")
        return exe, start, diagnostic, accessibility, example


if __name__ == "__main__":
    unittest.main()
