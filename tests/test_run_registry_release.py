import tempfile
import time
import unittest
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.release_package import build_windows_package, sha256_file
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
            exe = root / "Autosport.exe"
            start = root / "WINDOWS_START_HERE.txt"
            diagnostic = root / "diag.json"
            example = root / "example"
            example.mkdir()
            exe.write_bytes(b"fake-exe-for-package-test")
            start.write_text("start\n", encoding="utf-8")
            diagnostic.write_text('{"status":"PASS"}\n', encoding="utf-8")
            (example / "data.jsonl").write_text("{}\n", encoding="utf-8")
            first = root / "first.zip"
            second = root / "second.zip"
            _, first_hash = build_windows_package(exe, start, example, diagnostic, first, "c" * 40)
            time.sleep(0.01)
            start.touch()
            _, second_hash = build_windows_package(exe, start, example, diagnostic, second, "c" * 40)
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(sha256_file(first), sha256_file(second))


if __name__ == "__main__":
    unittest.main()
