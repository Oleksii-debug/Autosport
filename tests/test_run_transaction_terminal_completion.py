import json
import tempfile
import unittest
from pathlib import Path

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction, RunTransactionError


class RunTransactionTerminalCompletionTests(unittest.TestCase):
    @staticmethod
    def _prepare_canonical_commit(root: Path):
        registry = RunRegistry.initialize_pristine(root / "run_registry.json")
        book_path = root / "paper_book.json"
        PaperBook("10000").save(book_path)
        ledger = JsonlDecisionLedger(root / "decisions.jsonl")
        ledger.path.touch()

        market_sha256 = "a" * 64
        results_sha256 = "b" * 64
        strategy_id = "baseline-v1"
        run_id = "terminal-completion-run"
        base_book_hash = sha256_file(book_path)
        base_ledger_hash = sha256_file(ledger.path)
        experiment_key = registry.begin(
            market_sha256,
            results_sha256,
            strategy_id,
            run_id,
            base_paper_book_sha256=base_book_hash,
            base_decision_ledger_sha256=base_ledger_hash,
        )
        tx = RunTransaction.start(
            root,
            run_id=run_id,
            experiment_key=experiment_key,
            market_sha256=market_sha256,
            results_sha256=results_sha256,
            strategy_id=strategy_id,
            base_paper_book_sha256=base_book_hash,
            base_decision_ledger_sha256=base_ledger_hash,
        )
        tx.stage_outputs(PaperBook("10001"), ledger.path)
        summary = tx.precommit(
            {
                "schema_version": 2,
                "run_id": run_id,
                "experiment_key": experiment_key,
                "market_sha256": market_sha256,
                "sealed_results_sha256": results_sha256,
                "strategy_id": strategy_id,
                "real_money_execution": False,
            }
        )
        summary_path = tx.commit()
        return tx, registry, experiment_key, summary, summary_path

    def test_detached_completion_binds_completed_registry_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, registry, key, _summary, summary_path = self._prepare_canonical_commit(root)
            registry.reconcile_completed_summary(key, summary_path, root / "paper_book.json")

            detached = RunTransaction(root, tx.run_id)
            detached.mark_registry_completed()

            manifest = json.loads(detached.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "completed")
            self.assertEqual(registry.get(key)["status"], "completed")

            # Terminal completion is idempotent after a crash/retry at this boundary.
            detached.mark_registry_completed()
            self.assertEqual(
                json.loads(detached.manifest_path.read_text(encoding="utf-8"))["phase"],
                "completed",
            )

    def test_detached_completion_rejects_in_progress_registry_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, registry, key, _summary, _summary_path = self._prepare_canonical_commit(root)

            detached = RunTransaction(root, tx.run_id)
            with self.assertRaisesRegex(
                RunTransactionError,
                "transaction completion requires a completed registry identity",
            ):
                detached.mark_registry_completed()

            manifest = json.loads(detached.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "canonical_committed")
            self.assertEqual(registry.get(key)["status"], "in_progress")

    def test_detached_completion_missing_registry_fails_without_recreation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, registry, key, _summary, summary_path = self._prepare_canonical_commit(root)
            registry.reconcile_completed_summary(key, summary_path, root / "paper_book.json")

            registry_path = root / "run_registry.json"
            durable_paths = (
                tx.manifest_path,
                root / "paper_book.json",
                root / "decisions.jsonl",
                summary_path,
            )
            durable_before = {path: path.read_bytes() for path in durable_paths}
            registry_path.unlink()

            detached = RunTransaction(root, tx.run_id)
            with self.assertRaisesRegex(
                RunTransactionError,
                "transaction completion cannot validate terminal registry identity",
            ):
                detached.mark_registry_completed()

            self.assertFalse(registry_path.exists())
            self.assertEqual(
                {path: path.read_bytes() for path in durable_paths},
                durable_before,
            )

    def test_detached_completion_rejects_terminal_registry_new_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, registry, key, _summary, summary_path = self._prepare_canonical_commit(root)
            registry.reconcile_completed_summary(key, summary_path, root / "paper_book.json")

            registry_path = root / "run_registry.json"
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
            payload["runs"][key]["paper_book_sha256"] = "f" * 64
            registry_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            detached = RunTransaction(root, tx.run_id)
            with self.assertRaisesRegex(
                RunTransactionError,
                "completed registry evidence mismatch: paper_book_sha256",
            ):
                detached.mark_registry_completed()

            manifest = json.loads(detached.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "canonical_committed")


if __name__ == "__main__":
    unittest.main()
