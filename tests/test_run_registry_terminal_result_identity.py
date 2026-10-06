import json
import tempfile
import unittest
from pathlib import Path

from autosport.run_registry import RunRegistry


class RunRegistryTerminalResultIdentityTests(unittest.TestCase):
    @staticmethod
    def _completed_transaction_run(root: Path, *, run_id: str = "run-1") -> tuple[Path, str]:
        registry_path = root / "run_registry.json"
        registry = RunRegistry.initialize_pristine(registry_path)
        key = registry.begin(
            "a" * 64,
            "b" * 64,
            "strategy",
            run_id,
            base_paper_book_sha256="c" * 64,
            base_decision_ledger_sha256="d" * 64,
        )
        registry.complete(
            key,
            str(root / f"run-{run_id}.json"),
            paper_book_sha256="e" * 64,
            decision_ledger_sha256="f" * 64,
        )
        return registry_path, key

    def test_base_completed_entry_rejects_tampered_run_id_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path, key = self._completed_transaction_run(root)
            raw = json.loads(registry_path.read_text(encoding="utf-8"))
            raw["runs"][key]["run_id"] = "forged-run"
            registry_path.write_text(json.dumps(raw), encoding="utf-8")
            tampered_bytes = registry_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "result_path.*run_id"):
                RunRegistry(registry_path)

            self.assertEqual(registry_path.read_bytes(), tampered_bytes)

    def test_completed_entry_rejects_nonempty_mismatched_result_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path, key = self._completed_transaction_run(root)
            raw = json.loads(registry_path.read_text(encoding="utf-8"))
            raw["runs"][key]["result_path"] = str(root / "run-other.json")
            registry_path.write_text(json.dumps(raw), encoding="utf-8")
            tampered_bytes = registry_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "result_path.*run_id"):
                RunRegistry(registry_path)

            self.assertEqual(registry_path.read_bytes(), tampered_bytes)

    def test_transaction_aware_relative_result_path_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / "run_registry.json"
            registry = RunRegistry.initialize_pristine(registry_path)
            key = registry.begin(
                "a" * 64,
                "b" * 64,
                "strategy",
                "run-1",
                base_paper_book_sha256="c" * 64,
                base_decision_ledger_sha256="d" * 64,
            )
            registry.complete(
                key,
                "run-run-1.json",
                paper_book_sha256="e" * 64,
                decision_ledger_sha256="f" * 64,
            )

            self.assertEqual(RunRegistry(registry_path).get(key)["status"], "completed")

    def test_persisted_windows_result_path_is_portable_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path, key = self._completed_transaction_run(root)
            raw = json.loads(registry_path.read_text(encoding="utf-8"))
            raw["runs"][key]["result_path"] = r"C:\autosport\workspace\run-run-1.json"
            registry_path.write_text(json.dumps(raw), encoding="utf-8")

            reopened = RunRegistry(registry_path).get(key)
            self.assertEqual(reopened["status"], "completed")
            self.assertEqual(
                reopened["result_path"],
                r"C:\autosport\workspace\run-run-1.json",
            )

    def test_legacy_completed_entry_keeps_arbitrary_result_path_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / "run_registry.json"
            registry = RunRegistry.initialize_pristine(registry_path)
            key = registry.begin("a" * 64, "b" * 64, "strategy", "legacy-run")
            registry.complete(key, "legacy-summary.json")

            reopened = RunRegistry(registry_path).get(key)
            self.assertEqual(reopened["status"], "completed")
            self.assertEqual(reopened["result_path"], "legacy-summary.json")


if __name__ == "__main__":
    unittest.main()
