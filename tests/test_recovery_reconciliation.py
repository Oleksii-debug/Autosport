import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.cli import run_repair_workspace
from autosport.dataset import load_dataset
from autosport.integrity import sha256_file
from autosport.recovery import reconcile_late_crashes
from autosport.run_registry import (
    ReconciliationError,
    RepeatedExperimentError,
    RunRegistry,
)
from autosport.session import AutosportSession


class RecoveryReconciliationTests(unittest.TestCase):
    def _create_late_crash(self, workspace: str):
        dataset = load_dataset(Path("examples/tt_demo"))
        session = AutosportSession(workspace, "10000")
        with patch.object(
            session.registry,
            "complete",
            side_effect=RuntimeError("simulated crash after durable summary"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                session.run_dataset(dataset)
        unresolved = session.registry.in_progress()
        self.assertEqual(len(unresolved), 1)
        key, item = unresolved[0]
        summary_path = Path(workspace) / f"run-{item['run_id']}.json"
        self.assertTrue(summary_path.is_file())
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["schema_version"], 2)
        self.assertEqual(summary["experiment_key"], key)
        self.assertEqual(summary["paper_book_sha256"], sha256_file(session.book_path))
        session.close()
        return dataset, key, item, summary_path

    def test_late_crash_reconciles_without_replaying_economic_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset, key, _item, _summary = self._create_late_crash(tmp)
            book_hash_before = sha256_file(Path(tmp) / "paper_book.json")
            report = reconcile_late_crashes(tmp)
            self.assertEqual(report.reconciled_keys, (key,))
            self.assertEqual(report.unresolved_without_summary, ())
            self.assertEqual(sha256_file(Path(tmp) / "paper_book.json"), book_hash_before)

            registry = RunRegistry(Path(tmp) / "run_registry.json")
            repaired = registry.get(key)
            self.assertEqual(repaired["status"], "completed")
            self.assertTrue(repaired["reconciled_from_summary"])
            self.assertEqual(repaired["paper_book_sha256"], book_hash_before)

            restored = AutosportSession(tmp, "1")
            with self.assertRaises(RepeatedExperimentError):
                restored.run_dataset(dataset)
            restored.close()

    def test_tampered_paper_book_prevents_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _dataset, key, _item, _summary = self._create_late_crash(tmp)
            book_path = Path(tmp) / "paper_book.json"
            book_path.write_text(book_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(ReconciliationError, "PaperBook SHA-256"):
                reconcile_late_crashes(tmp)
            self.assertEqual(RunRegistry(Path(tmp) / "run_registry.json").get(key)["status"], "in_progress")

    def test_summary_identity_mismatch_prevents_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _dataset, key, _item, summary_path = self._create_late_crash(tmp)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["strategy_id"] = "tampered-strategy"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(ReconciliationError, "identity mismatch"):
                reconcile_late_crashes(tmp)
            self.assertEqual(RunRegistry(Path(tmp) / "run_registry.json").get(key)["status"], "in_progress")

    def test_missing_summary_remains_unresolved_and_cli_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            key = registry.begin("a" * 64, "b" * 64, "strategy", "run-missing-summary")
            report = reconcile_late_crashes(root)
            self.assertEqual(report.reconciled_keys, ())
            self.assertEqual(report.unresolved_without_summary, (key,))
            self.assertEqual(run_repair_workspace(root), 4)
            self.assertEqual(registry.get(key)["status"], "in_progress")

    def test_empty_workspace_repair_is_noop_without_creating_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = reconcile_late_crashes(root)
            self.assertEqual(report.reconciled_keys, ())
            self.assertEqual(report.unresolved_without_summary, ())
            self.assertFalse((root / "run_registry.json").exists())
            self.assertEqual(run_repair_workspace(root), 0)


if __name__ == "__main__":
    unittest.main()
