import json
import tempfile
import unittest
from pathlib import Path

from autosport.integrity import sha256_file
from autosport.run_registry import RunRegistry


class RunRegistryReconciledResultPathIntegrityTests(unittest.TestCase):
    @staticmethod
    def _reconciled_legacy_run(root: Path) -> tuple[Path, str]:
        registry_path = root / "run_registry.json"
        registry = RunRegistry.initialize_pristine(registry_path)
        key = registry.begin(
            "a" * 64,
            "b" * 64,
            "strategy",
            "run-1",
        )

        book_path = root / "paper_book.json"
        book_path.write_text("{}\n", encoding="utf-8")
        summary_path = root / "run-run-1.json"
        summary_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "experiment_key": key,
                    "run_id": "run-1",
                    "market_sha256": "a" * 64,
                    "sealed_results_sha256": "b" * 64,
                    "strategy_id": "strategy",
                    "real_money_execution": False,
                    "paper_book_sha256": sha256_file(book_path),
                }
            ),
            encoding="utf-8",
        )

        registry.reconcile_completed_summary(key, summary_path, book_path)
        reconciled = registry.get(key)
        if not reconciled.get("reconciled_from_summary"):
            raise AssertionError("test fixture did not create reconciled registry evidence")
        if "base_paper_book_sha256" in reconciled or "base_decision_ledger_sha256" in reconciled:
            raise AssertionError("test fixture unexpectedly contains BASE transaction evidence")
        return registry_path, key

    def test_legacy_reconciled_completed_entry_rejects_empty_result_path_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path, key = self._reconciled_legacy_run(root)
            raw = json.loads(registry_path.read_text(encoding="utf-8"))
            raw["runs"][key]["result_path"] = ""
            registry_path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "reconciled run registry entry lacks result_path evidence",
            ):
                RunRegistry(registry_path)

    def test_legacy_reconciled_completed_entry_rejects_tampered_run_id_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path, key = self._reconciled_legacy_run(root)
            raw = json.loads(registry_path.read_text(encoding="utf-8"))
            raw["runs"][key]["run_id"] = "forged-run"
            registry_path.write_text(json.dumps(raw), encoding="utf-8")
            tampered_bytes = registry_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "reconciled.*result_path.*run_id"):
                RunRegistry(registry_path)

            self.assertEqual(registry_path.read_bytes(), tampered_bytes)


if __name__ == "__main__":
    unittest.main()
