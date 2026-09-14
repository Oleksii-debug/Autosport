import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.dataset import load_dataset
from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.recovery import reconcile_late_crashes
from autosport.run_registry import ReconciliationError, RunRegistry
from autosport.run_transaction import RunTransaction, RunTransactionError
from autosport.session import AutosportSession


class TransactionRecoveryTests(unittest.TestCase):
    def _dataset(self):
        return load_dataset(Path("examples/tt_demo"))

    def _in_progress(self, root: Path):
        registry = RunRegistry(root / "run_registry.json")
        unresolved = registry.in_progress()
        self.assertEqual(len(unresolved), 1)
        return registry, unresolved[0][0], unresolved[0][1]

    @staticmethod
    def _write_corrupt_ledger(root: Path) -> Path:
        ledger = JsonlDecisionLedger(root / "decisions.jsonl")
        ledger.append(
            DecisionRecord(
                "prior-run",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "OBSERVE",
                {"x": 1},
                "ctx",
            )
        )
        envelope = json.loads(ledger.path.read_text(encoding="utf-8"))
        envelope["record"]["payload"]["x"] = 2
        ledger.path.write_text(
            json.dumps(envelope, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return ledger.path

    @staticmethod
    def _start_transaction(root: Path, run_id: str) -> tuple[RunTransaction, dict]:
        book_path = root / "paper_book.json"
        PaperBook("10000").save(book_path)
        ledger_path = TransactionRecoveryTests._write_corrupt_ledger(root)
        market_hash = "a" * 64
        results_hash = "b" * 64
        item = {
            "run_id": run_id,
            "market_sha256": market_hash,
            "results_sha256": results_hash,
            "strategy_id": "baseline-v1",
        }
        tx = RunTransaction.start(
            root,
            run_id=run_id,
            experiment_key="experiment",
            market_sha256=market_hash,
            results_sha256=results_hash,
            strategy_id="baseline-v1",
            base_paper_book_sha256=sha256_file(book_path),
            base_decision_ledger_sha256=sha256_file(ledger_path),
        )
        return tx, item

    def test_runtime_failure_before_precommit_aborts_without_canonical_economic_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = AutosportSession(root, "10000")
            with patch.object(
                RunTransaction,
                "stage_outputs",
                side_effect=RuntimeError("simulated runtime failure before precommit"),
            ):
                with self.assertRaisesRegex(RuntimeError, "before precommit"):
                    session.run_dataset(self._dataset())

            registry = RunRegistry(root / "run_registry.json")
            self.assertEqual(registry.in_progress(), ())
            registry_state = json.loads(
                (root / "run_registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(registry_state["runs"]), 1)
            key, item = next(iter(registry_state["runs"].items()))
            self.assertEqual(item["status"], "aborted")
            base_book_hash = item["base_paper_book_sha256"]
            base_ledger_hash = item["base_decision_ledger_sha256"]
            self.assertEqual(sha256_file(root / "paper_book.json"), base_book_hash)
            self.assertEqual(sha256_file(root / "decisions.jsonl"), base_ledger_hash)
            self.assertEqual((root / "decisions.jsonl").read_text(encoding="utf-8"), "")
            tx = RunTransaction(root, str(item["run_id"]))
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "aborted")
            session.close()

            # Already-proven ordinary failures must not require or be reclassified by
            # crash repair; repair is reserved for genuinely unresolved runs.
            report = reconcile_late_crashes(root)
            self.assertEqual(report.reconciled_keys, ())
            self.assertEqual(report.aborted_uncommitted_keys, ())
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

    def test_stage_outputs_rejects_hash_matching_corrupt_base_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, _item = self._start_transaction(root, "corrupt-stage")

            with self.assertRaisesRegex(
                RunTransactionError,
                "canonical Decision Ledger integrity validation failed",
            ):
                tx.stage_outputs(
                    PaperBook.load(root / "paper_book.json"),
                    root / "decisions.jsonl",
                )
            self.assertFalse(tx.staged_book_path.exists())

    def test_transaction_recovery_rejects_hash_matching_corrupt_base_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, item = self._start_transaction(root, "corrupt-recovery")

            with self.assertRaisesRegex(
                RunTransactionError,
                "canonical Decision Ledger integrity validation failed",
            ):
                RunTransaction.recover(
                    root,
                    run_id="corrupt-recovery",
                    registry_item=item,
                    experiment_key="experiment",
                )
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "staging")

    def test_pre_manifest_recovery_rejects_hash_matching_corrupt_base_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = root / "paper_book.json"
            PaperBook("10000").save(book_path)
            ledger_path = self._write_corrupt_ledger(root)
            registry = RunRegistry(root / "run_registry.json")
            key = registry.begin(
                "a" * 64,
                "b" * 64,
                "baseline-v1",
                "crash-before-manifest",
                base_paper_book_sha256=sha256_file(book_path),
                base_decision_ledger_sha256=sha256_file(ledger_path),
            )

            with self.assertRaisesRegex(
                ReconciliationError,
                "canonical Decision Ledger integrity validation failed",
            ):
                reconcile_late_crashes(root)
            self.assertEqual(registry.get(key)["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
