import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.dataset import load_dataset
from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.recovery import reconcile_late_crashes
from autosport.run_registry import ReconciliationError, RunRegistry
from autosport.run_transaction import RunTransaction
from autosport.session import AutosportSession


class TransactionRecoveryTests(unittest.TestCase):
    def _dataset(self):
        return load_dataset(Path("examples/tt_demo"))

    def _in_progress(self, root: Path):
        registry = RunRegistry(root / "run_registry.json")
        unresolved = registry.in_progress()
        self.assertEqual(len(unresolved), 1)
        return registry, unresolved[0][0], unresolved[0][1]

    def test_crash_before_precommit_aborts_without_canonical_economic_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = AutosportSession(root, "10000")
            with patch.object(
                RunTransaction,
                "stage_outputs",
                side_effect=RuntimeError("simulated crash before precommit"),
            ):
                with self.assertRaisesRegex(RuntimeError, "before precommit"):
                    session.run_dataset(self._dataset())

            registry, key, item = self._in_progress(root)
            base_book_hash = item["base_paper_book_sha256"]
            base_ledger_hash = item["base_decision_ledger_sha256"]
            self.assertEqual(sha256_file(root / "paper_book.json"), base_book_hash)
            self.assertEqual(sha256_file(root / "decisions.jsonl"), base_ledger_hash)
            self.assertEqual((root / "decisions.jsonl").read_text(encoding="utf-8"), "")
            session.close()

            report = reconcile_late_crashes(root)
            self.assertEqual(report.reconciled_keys, ())
            self.assertEqual(report.aborted_uncommitted_keys, (key,))
            self.assertEqual(report.unresolved_without_summary, ())
            self.assertEqual(registry.get(key)["status"], "aborted")

            retry = AutosportSession(root, "1")
            result = retry.run_dataset(self._dataset())
            retry.close()
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, result.balance)
            self.assertEqual(len((root / "decisions.jsonl").read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(RunRegistry(root / "run_registry.json").get(result.experiment_key)["status"], "completed")

    def test_precommitted_run_is_completed_by_recovery_without_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = AutosportSession(root, "10000")
            with patch.object(
                RunTransaction,
                "commit",
                side_effect=RuntimeError("simulated crash after precommit"),
            ):
                with self.assertRaisesRegex(RuntimeError, "after precommit"):
                    session.run_dataset(self._dataset())
            registry, key, item = self._in_progress(root)
            tx = RunTransaction(root, str(item["run_id"]))
            self.assertTrue(tx.manifest_path.is_file())
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10000)
            self.assertEqual((root / "decisions.jsonl").read_text(encoding="utf-8"), "")
            session.close()

            report = reconcile_late_crashes(root)
            self.assertEqual(report.reconciled_keys, (key,))
            self.assertEqual(report.aborted_uncommitted_keys, ())
            self.assertEqual(report.unresolved_without_summary, ())
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10031)
            self.assertEqual(len((root / "decisions.jsonl").read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(registry.get(key)["status"], "completed")

    def test_partial_book_promotion_recovers_remaining_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = AutosportSession(root, "10000")
            with patch.object(
                RunTransaction,
                "commit",
                side_effect=RuntimeError("stop after precommit"),
            ):
                with self.assertRaises(RuntimeError):
                    session.run_dataset(self._dataset())
            registry, key, item = self._in_progress(root)
            tx = RunTransaction(root, str(item["run_id"]))

            # Simulate a process dying after the first os.replace: PaperBook is NEW,
            # Decision Ledger and summary are still BASE/absent.
            os.replace(tx.staged_book_path, root / "paper_book.json")
            session.close()

            report = reconcile_late_crashes(root)
            self.assertEqual(report.reconciled_keys, (key,))
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10031)
            self.assertEqual(len((root / "decisions.jsonl").read_text(encoding="utf-8").splitlines()), 1)
            self.assertTrue((root / f"run-{item['run_id']}.json").is_file())
            self.assertEqual(registry.get(key)["status"], "completed")

    def test_tampered_staged_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = AutosportSession(root, "10000")
            with patch.object(
                RunTransaction,
                "commit",
                side_effect=RuntimeError("stop after precommit"),
            ):
                with self.assertRaises(RuntimeError):
                    session.run_dataset(self._dataset())
            registry, key, item = self._in_progress(root)
            tx = RunTransaction(root, str(item["run_id"]))
            with tx.staged_book_path.open("ab") as handle:
                handle.write(b"tamper")
            session.close()

            with self.assertRaisesRegex(ReconciliationError, "staged PaperBook artifact hash mismatch"):
                reconcile_late_crashes(root)
            self.assertEqual(registry.get(key)["status"], "in_progress")
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10000)


if __name__ == "__main__":
    unittest.main()
