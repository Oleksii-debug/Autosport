import json
import os
import tempfile
import unittest
from pathlib import Path

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.run_transaction import RunTransaction, RunTransactionError


class RunTransactionCanonicalTargetIndirectionTests(unittest.TestCase):
    @staticmethod
    def _prepare_precommitted(root: Path) -> RunTransaction:
        workspace = root / "workspace"
        workspace.mkdir()

        book_path = workspace / "paper_book.json"
        PaperBook("10000").save(book_path)
        ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
        ledger.path.touch()

        run_id = "canonical-target-indirection"
        tx = RunTransaction.start(
            workspace,
            run_id=run_id,
            experiment_key="experiment-key",
            market_sha256="a" * 64,
            results_sha256="b" * 64,
            strategy_id="baseline-v1",
            base_paper_book_sha256=sha256_file(book_path),
            base_decision_ledger_sha256=sha256_file(ledger.path),
        )
        JsonlDecisionLedger(tx.run_ledger_path).append(
            DecisionRecord(
                replay_run_id=run_id,
                agent="baseline-agent",
                observed_ts="2026-09-14T00:00:00Z",
                action="hold",
                payload={"reason": "regression"},
                context_hash="c" * 64,
            )
        )
        tx.stage_outputs(PaperBook("10001"), ledger.path)
        tx.precommit(
            {
                "schema_version": 2,
                "run_id": run_id,
                "experiment_key": "experiment-key",
                "market_sha256": "a" * 64,
                "sealed_results_sha256": "b" * 64,
                "strategy_id": "baseline-v1",
                "real_money_execution": False,
            }
        )
        return tx

    def _replace_with_symlink(self, target: Path, external: Path) -> None:
        target.unlink()
        try:
            target.symlink_to(external)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"file symlinks are unavailable on this platform: {exc}")

    def test_commit_rejects_symlinked_paper_book_even_when_target_has_exact_new_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = self._prepare_precommitted(root)
            book_path = tx.workspace / "paper_book.json"
            external = root / "external-paper-book.json"
            expected_external = tx.staged_book_path.read_bytes()
            external.write_bytes(expected_external)
            self._replace_with_symlink(book_path, external)

            with self.assertRaisesRegex(
                RunTransactionError,
                "PaperBook canonical path must be a regular non-symlink file",
            ):
                tx.commit()

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "precommitted")
            self.assertTrue(book_path.is_symlink())
            self.assertEqual(external.read_bytes(), expected_external)

    def test_commit_preflight_rejects_symlinked_ledger_before_paper_book_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = self._prepare_precommitted(root)
            book_path = tx.workspace / "paper_book.json"
            ledger_path = tx.workspace / "decisions.jsonl"
            base_book_bytes = book_path.read_bytes()
            external = root / "external-decisions.jsonl"
            expected_external = tx.staged_ledger_path.read_bytes()
            external.write_bytes(expected_external)
            self._replace_with_symlink(ledger_path, external)

            with self.assertRaisesRegex(
                RunTransactionError,
                "Decision Ledger canonical path must be a regular non-symlink file",
            ):
                tx.commit()

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "precommitted")
            self.assertEqual(book_path.read_bytes(), base_book_bytes)
            self.assertTrue(ledger_path.is_symlink())
            self.assertEqual(external.read_bytes(), expected_external)

    def test_commit_preflight_rejects_symlinked_run_summary_with_exact_new_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = self._prepare_precommitted(root)
            summary_path = tx.workspace / f"run-{tx.run_id}.json"
            external = root / "external-run-summary.json"
            expected_external = tx.staged_summary_path.read_bytes()
            external.write_bytes(expected_external)
            summary_path.symlink_to(external)

            with self.assertRaisesRegex(
                RunTransactionError,
                "canonical run summary canonical path must be a regular non-symlink file",
            ):
                tx.commit()

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "precommitted")
            self.assertTrue(summary_path.is_symlink())
            self.assertEqual(external.read_bytes(), expected_external)

    def test_commit_preflight_rejects_hardlinked_run_summary_with_exact_new_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = self._prepare_precommitted(root)
            summary_path = tx.workspace / f"run-{tx.run_id}.json"
            book_path = tx.workspace / "paper_book.json"
            ledger_path = tx.workspace / "decisions.jsonl"
            base_book_bytes = book_path.read_bytes()
            base_ledger_bytes = ledger_path.read_bytes()
            external = root / "external-run-summary.json"
            expected_external = tx.staged_summary_path.read_bytes()
            external.write_bytes(expected_external)
            try:
                os.link(external, summary_path)
            except OSError as exc:
                self.skipTest(f"hard links are unavailable on this platform: {exc}")

            with self.assertRaisesRegex(
                RunTransactionError,
                "canonical run summary canonical path must not have hard-link aliases",
            ):
                tx.commit()

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "precommitted")
            self.assertEqual(book_path.read_bytes(), base_book_bytes)
            self.assertEqual(ledger_path.read_bytes(), base_ledger_bytes)
            self.assertEqual(summary_path.read_bytes(), expected_external)
            self.assertEqual(external.read_bytes(), expected_external)

    def test_regular_already_new_commit_retry_remains_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = self._prepare_precommitted(root)

            summary_path = tx.commit()
            book_path = tx.workspace / "paper_book.json"
            ledger_path = tx.workspace / "decisions.jsonl"
            first_book_bytes = book_path.read_bytes()
            first_ledger_bytes = ledger_path.read_bytes()

            self.assertFalse(book_path.is_symlink())
            self.assertFalse(ledger_path.is_symlink())
            self.assertFalse(summary_path.is_symlink())
            self.assertEqual(
                json.loads(tx.manifest_path.read_text(encoding="utf-8"))["phase"],
                "canonical_committed",
            )

            self.assertEqual(tx.commit(), summary_path)
            self.assertEqual(book_path.read_bytes(), first_book_bytes)
            self.assertEqual(ledger_path.read_bytes(), first_ledger_bytes)
            self.assertFalse(summary_path.is_symlink())
            self.assertEqual(
                json.loads(tx.manifest_path.read_text(encoding="utf-8"))["phase"],
                "canonical_committed",
            )
