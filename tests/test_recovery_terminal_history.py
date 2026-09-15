import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.integrity import sha256_file
from autosport.recovery import reconcile_late_crashes
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction
from autosport.session import AutosportSession


class RecoveryTerminalHistoryTests(unittest.TestCase):
    def test_completed_historical_transaction_survives_later_valid_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = load_dataset(Path("examples/tt_demo"))
            session = AutosportSession(root, "10000")

            first = session.run_dataset(dataset)
            first_registry = session.registry.get(first.experiment_key)
            first_book_hash = first_registry["paper_book_sha256"]
            first_manifest_path = (
                root
                / RunTransaction.ROOT_NAME
                / first.replay.run_id
                / "manifest.json"
            )
            self.assertEqual(
                json.loads(first_manifest_path.read_text(encoding="utf-8"))["phase"],
                "completed",
            )

            second = session.run_dataset(dataset, allow_repeat=True)
            session.close()

            current_book_hash = sha256_file(root / "paper_book.json")
            current_ledger_hash = sha256_file(root / "decisions.jsonl")
            registry_before = (root / "run_registry.json").read_bytes()
            first_manifest_before = first_manifest_path.read_bytes()
            second_manifest_path = (
                root
                / RunTransaction.ROOT_NAME
                / second.replay.run_id
                / "manifest.json"
            )
            second_manifest_before = second_manifest_path.read_bytes()

            self.assertNotEqual(current_book_hash, first_book_hash)

            report = reconcile_late_crashes(root)

            self.assertEqual(report.reconciled_keys, ())
            self.assertEqual(report.aborted_uncommitted_keys, ())
            self.assertEqual(report.unresolved_without_summary, ())
            self.assertEqual(sha256_file(root / "paper_book.json"), current_book_hash)
            self.assertEqual(sha256_file(root / "decisions.jsonl"), current_ledger_hash)
            self.assertEqual((root / "run_registry.json").read_bytes(), registry_before)
            self.assertEqual(first_manifest_path.read_bytes(), first_manifest_before)
            self.assertEqual(second_manifest_path.read_bytes(), second_manifest_before)

            registry = RunRegistry(root / "run_registry.json")
            self.assertEqual(registry.get(first.experiment_key)["status"], "completed")
            self.assertEqual(registry.get(second.experiment_key)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
