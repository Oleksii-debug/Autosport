import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

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

    @staticmethod
    def _summary_payload(run_id: str = "current-run", **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 2,
            "run_id": run_id,
            "experiment_key": "experiment",
            "market_sha256": "a" * 64,
            "sealed_results_sha256": "b" * 64,
            "strategy_id": "baseline-v1",
            "real_money_execution": False,
        }
        payload.update(overrides)
        return payload

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

    def test_stage_outputs_rejects_semantically_invalid_paper_book_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, book_path, ledger_path = self._start_transaction(root, "current-run")
            book = PaperBook.load(book_path)
            book.balance = Decimal("-1")

            with self.assertRaisesRegex(
                RunTransactionError,
                "staged PaperBook semantic validation failed",
            ):
                tx.stage_outputs(book, ledger_path)

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "staging")
            self.assertNotIn("staged_snapshot", manifest)

    def test_stage_outputs_accepts_current_run_suffix_after_prior_run_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, book_path, ledger_path = self._start_transaction(
                root,
                "current-run",
                prior_run_id="prior-run",
            )
            self._append_run_decision(tx, "current-run")

            staged_book_hash, staged_ledger_hash = tx.stage_outputs(
                PaperBook.load(book_path),
                ledger_path,
            )

            staged_snapshot = JsonlDecisionLedger(tx.staged_ledger_path).verified_snapshot()
            self.assertEqual(staged_snapshot.record_count, 2)
            self.assertEqual(staged_snapshot.sha256, staged_ledger_hash)
            self.assertEqual(JsonlDecisionLedger(ledger_path).verify_integrity(), 1)
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["staged_snapshot"]["paper_book_sha256"], staged_book_hash)
            self.assertEqual(
                manifest["staged_snapshot"]["decision_ledger_sha256"],
                staged_ledger_hash,
            )

            summary = tx.precommit(self._summary_payload())
            self.assertEqual(summary["run_id"], "current-run")
            self.assertEqual(summary["transaction_run_id"], "current-run")
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "precommitted")

    def test_precommit_rejects_post_stage_paper_book_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, book_path, ledger_path = self._start_transaction(root, "current-run")
            tx.stage_outputs(PaperBook.load(book_path), ledger_path)

            replacement = PaperBook("20000")
            replacement.save(tx.staged_book_path)

            with self.assertRaisesRegex(
                RunTransactionError,
                "staged PaperBook changed after stage_outputs",
            ):
                tx.precommit(self._summary_payload())

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "staging")
            self.assertFalse(tx.staged_summary_path.exists())

    def test_precommit_rejects_semantically_invalid_post_stage_paper_book_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, book_path, ledger_path = self._start_transaction(root, "current-run")
            tx.stage_outputs(PaperBook.load(book_path), ledger_path)

            replacement = json.loads(tx.staged_book_path.read_text(encoding="utf-8"))
            replacement["balance"] = "-1"
            tx.staged_book_path.write_text(
                json.dumps(replacement, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "staged PaperBook semantic validation failed",
            ):
                tx.precommit(self._summary_payload())

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "staging")
            self.assertFalse(tx.staged_summary_path.exists())

    def test_precommit_rejects_post_stage_foreign_combined_ledger_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, book_path, ledger_path = self._start_transaction(root, "current-run")
            self._append_run_decision(tx, "current-run")
            tx.stage_outputs(PaperBook.load(book_path), ledger_path)

            forged = root / "forged.jsonl"
            JsonlDecisionLedger(forged).append(
                DecisionRecord(
                    "foreign-run",
                    "paper-agent",
                    "2026-01-02T00:00:00+00:00",
                    "OPEN_PAPER_TICKET",
                    {"ticket_id": "forged"},
                    "foreign-context",
                )
            )
            tx.staged_ledger_path.write_bytes(forged.read_bytes())

            with self.assertRaisesRegex(
                RunTransactionError,
                "combined staged Decision Ledger changed after stage_outputs",
            ):
                tx.precommit(self._summary_payload())

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "staging")
            self.assertFalse(tx.staged_summary_path.exists())

    def test_precommit_rejects_run_suffix_mutation_after_stage_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, book_path, ledger_path = self._start_transaction(root, "current-run")
            self._append_run_decision(tx, "current-run")
            tx.stage_outputs(PaperBook.load(book_path), ledger_path)
            self._append_run_decision(tx, "current-run")

            with self.assertRaisesRegex(
                RunTransactionError,
                "staged run Decision Ledger changed after stage_outputs",
            ):
                tx.precommit(self._summary_payload())

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "staging")

    def test_precommit_rejects_summary_identity_mismatch_before_durable_precommit(self):
        cases = {
            "run_id": {"run_id": "foreign-run"},
            "experiment_key": {"experiment_key": "foreign-experiment"},
            "market_sha256": {"market_sha256": "c" * 64},
            "sealed_results_sha256": {"sealed_results_sha256": "d" * 64},
            "strategy_id": {"strategy_id": "observe-only-v1"},
            "real_money_execution": {"real_money_execution": True},
        }
        for field_name, overrides in cases.items():
            with self.subTest(field_name=field_name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                tx, book_path, ledger_path = self._start_transaction(root, "current-run")
                tx.stage_outputs(PaperBook.load(book_path), ledger_path)

                with self.assertRaisesRegex(
                    RunTransactionError,
                    rf"staged run summary transaction identity mismatch: .*{field_name}",
                ):
                    tx.precommit(self._summary_payload(**overrides))

                manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["phase"], "staging")
                self.assertFalse(tx.staged_summary_path.exists())
                self.assertFalse((root / "run-current-run.json").exists())

    def test_commit_rejects_rehashed_summary_with_foreign_manifest_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, book_path, ledger_path = self._start_transaction(root, "current-run")
            base_book_hash = sha256_file(book_path)
            base_ledger_hash = sha256_file(ledger_path)
            tx.stage_outputs(PaperBook.load(book_path), ledger_path)
            tx.precommit(self._summary_payload())

            summary = json.loads(tx.staged_summary_path.read_text(encoding="utf-8"))
            summary["strategy_id"] = "observe-only-v1"
            tx.staged_summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            manifest["new"]["summary_sha256"] = sha256_file(tx.staged_summary_path)
            tx.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "staged run summary transaction identity mismatch: strategy_id",
            ):
                tx.commit()

            self.assertEqual(sha256_file(book_path), base_book_hash)
            self.assertEqual(sha256_file(ledger_path), base_ledger_hash)
            self.assertFalse((root / "run-current-run.json").exists())

    def test_commit_rejects_rehashed_semantically_invalid_paper_book_before_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, book_path, ledger_path = self._start_transaction(root, "current-run")
            base_book_hash = sha256_file(book_path)
            base_ledger_hash = sha256_file(ledger_path)
            tx.stage_outputs(PaperBook.load(book_path), ledger_path)
            tx.precommit(self._summary_payload())

            tampered_book = json.loads(tx.staged_book_path.read_text(encoding="utf-8"))
            tampered_book["balance"] = "-1"
            tx.staged_book_path.write_text(
                json.dumps(tampered_book, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tampered_book_hash = sha256_file(tx.staged_book_path)

            summary = json.loads(tx.staged_summary_path.read_text(encoding="utf-8"))
            summary["paper_book_sha256"] = tampered_book_hash
            tx.staged_summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            manifest["new"]["paper_book_sha256"] = tampered_book_hash
            manifest["new"]["summary_sha256"] = sha256_file(tx.staged_summary_path)
            tx.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "staged PaperBook semantic validation failed",
            ):
                tx.commit()

            self.assertEqual(sha256_file(book_path), base_book_hash)
            self.assertEqual(sha256_file(ledger_path), base_ledger_hash)
            self.assertFalse((root / "run-current-run.json").exists())

    def test_replace_verified_promotes_captured_bytes_when_staged_path_swaps_at_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "staged.bin"
            target = root / "target.bin"
            staged.write_bytes(b"verified-original")
            target.write_bytes(b"base")
            expected_hash = sha256_file(staged)
            real_replace = os.replace
            swapped = False

            def swap_staged_then_replace(source: str | bytes | os.PathLike, destination: str | bytes | os.PathLike) -> None:
                nonlocal swapped
                staged.write_bytes(b"tampered-after-snapshot")
                swapped = True
                real_replace(source, destination)

            with patch("autosport.run_transaction.os.replace", side_effect=swap_staged_then_replace):
                RunTransaction._replace_verified(staged, target, expected_hash, "test artifact")

            self.assertTrue(swapped)
            self.assertEqual(target.read_bytes(), b"verified-original")
            self.assertEqual(staged.read_bytes(), b"tampered-after-snapshot")


if __name__ == "__main__":
    unittest.main()
