import tempfile
import unittest
from pathlib import Path

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.run_transaction import RunTransaction, RunTransactionError


class RunTransactionDecisionBindingTests(unittest.TestCase):
    @staticmethod
    def _start_transaction(
        root: Path,
        run_id: str,
        *,
        prior_run_id: str | None = None,
    ) -> tuple[RunTransaction, Path, Path]:
        book_path = root / "paper_book.json"
        PaperBook("10000").save(book_path)

        ledger = JsonlDecisionLedger(root / "decisions.jsonl")
        if prior_run_id is None:
            ledger.path.touch()
        else:
            ledger.append(
                DecisionRecord(
                    prior_run_id,
                    "prior-agent",
                    "2026-01-01T00:00:00+00:00",
                    "OBSERVE",
                    {"source": "prior"},
                    "prior-context",
                )
            )

        tx = RunTransaction.start(
            root,
            run_id=run_id,
            experiment_key="experiment",
            market_sha256="a" * 64,
            results_sha256="b" * 64,
            strategy_id="baseline-v1",
            base_paper_book_sha256=sha256_file(book_path),
            base_decision_ledger_sha256=sha256_file(ledger.path),
        )
        return tx, book_path, ledger.path

    @staticmethod
    def _append_run_decision(tx: RunTransaction, replay_run_id: str) -> None:
        JsonlDecisionLedger(tx.run_ledger_path).append(
            DecisionRecord(
                replay_run_id,
                "paper-agent",
                "2026-01-02T00:00:00+00:00",
                "OPEN_PAPER_TICKET",
                {"ticket_id": "ticket-1"},
                "run-context",
            )
        )

    def test_stage_outputs_rejects_foreign_run_decision_before_combining_ledgers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, book_path, ledger_path = self._start_transaction(root, "current-run")
            self._append_run_decision(tx, "foreign-run")

            with self.assertRaisesRegex(
                RunTransactionError,
                "staged run Decision Ledger replay_run_id mismatch at line 1",
            ):
                tx.stage_outputs(PaperBook.load(book_path), ledger_path)

            self.assertEqual(JsonlDecisionLedger(ledger_path).verify_integrity(), 0)
            self.assertFalse(tx.staged_ledger_path.exists())

    def test_stage_outputs_accepts_current_run_suffix_after_prior_run_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, book_path, ledger_path = self._start_transaction(
                root,
                "current-run",
                prior_run_id="prior-run",
            )
            self._append_run_decision(tx, "current-run")

            _book_hash, staged_ledger_hash = tx.stage_outputs(
                PaperBook.load(book_path),
                ledger_path,
            )

            staged_snapshot = JsonlDecisionLedger(tx.staged_ledger_path).verified_snapshot()
            self.assertEqual(staged_snapshot.record_count, 2)
            self.assertEqual(staged_snapshot.sha256, staged_ledger_hash)
            self.assertEqual(JsonlDecisionLedger(ledger_path).verify_integrity(), 1)


if __name__ == "__main__":
    unittest.main()
