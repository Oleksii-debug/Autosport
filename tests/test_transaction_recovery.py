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

    def _precommitted_transaction(self, root: Path) -> tuple[RunTransaction, dict]:
        session = AutosportSession(root, "10000")
        with patch.object(
            RunTransaction,
            "commit",
            side_effect=RuntimeError("stop after precommit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after precommit"):
                session.run_dataset(self._dataset())
        _registry, _key, item = self._in_progress(root)
        tx = RunTransaction(root, str(item["run_id"]))
        session.close()
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

    def test_commit_rejects_manifest_summary_ledger_misbinding_before_economic_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, item = self._precommitted_transaction(root)

            JsonlDecisionLedger(tx.staged_ledger_path).append(
                DecisionRecord(
                    str(item["run_id"]),
                    "adversarial-test",
                    "2026-01-01T00:00:01+00:00",
                    "OBSERVE",
                    {"extra": True},
                    "ctx-extra",
                )
            )
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            manifest["new"]["decision_ledger_sha256"] = sha256_file(tx.staged_ledger_path)
            tx.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "transaction binding mismatch: decision_ledger_sha256",
            ):
                tx.commit()
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10000)
            self.assertEqual((root / "decisions.jsonl").read_text(encoding="utf-8"), "")
            self.assertFalse((root / f"run-{item['run_id']}.json").exists())

    def test_commit_preflights_existing_summary_target_before_economic_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, item = self._precommitted_transaction(root)

            summary_target = root / f"run-{item['run_id']}.json"
            summary_target.write_text('{"poisoned":true}\n', encoding="utf-8")

            with self.assertRaisesRegex(
                RunTransactionError,
                "canonical run summary SHA-256 mismatch",
            ):
                tx.commit()
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10000)
            self.assertEqual((root / "decisions.jsonl").read_text(encoding="utf-8"), "")

    def test_commit_rejects_duplicate_manifest_keys_before_economic_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, _item = self._precommitted_transaction(root)
            manifest_text = tx.manifest_path.read_text(encoding="utf-8")
            needle = '  "phase": "precommitted",'
            self.assertIn(needle, manifest_text)
            tx.manifest_path.write_text(
                manifest_text.replace(
                    needle,
                    '  "phase": "completed",\n  "phase": "precommitted",',
                    1,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "transaction manifest contains duplicate JSON key 'phase'",
            ):
                tx.commit()
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10000)
            self.assertEqual((root / "decisions.jsonl").read_text(encoding="utf-8"), "")

    def test_commit_rejects_nonfinite_manifest_json_before_economic_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, _item = self._precommitted_transaction(root)
            manifest_text = tx.manifest_path.read_text(encoding="utf-8")
            tx.manifest_path.write_text(
                manifest_text.replace("{", '{\n  "ambiguous": NaN,', 1),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "transaction manifest contains non-finite JSON value 'NaN'",
            ):
                tx.commit()
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10000)
            self.assertEqual((root / "decisions.jsonl").read_text(encoding="utf-8"), "")

    def test_commit_rejects_boolean_manifest_schema_before_economic_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, _item = self._precommitted_transaction(root)
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = True
            tx.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "transaction manifest schema is invalid",
            ):
                tx.commit()
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10000)
            self.assertEqual((root / "decisions.jsonl").read_text(encoding="utf-8"), "")

    def test_commit_rejects_duplicate_summary_bindings_before_economic_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, _item = self._precommitted_transaction(root)
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            ledger_hash = manifest["new"]["decision_ledger_sha256"]
            summary_text = tx.staged_summary_path.read_text(encoding="utf-8")
            needle = f'  "decision_ledger_sha256": "{ledger_hash}",'
            self.assertIn(needle, summary_text)
            tx.staged_summary_path.write_text(
                summary_text.replace(
                    needle,
                    f'  "decision_ledger_sha256": "{"0" * 64}",\n{needle}',
                    1,
                ),
                encoding="utf-8",
            )
            manifest["new"]["summary_sha256"] = sha256_file(tx.staged_summary_path)
            tx.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "staged run summary contains duplicate JSON key 'decision_ledger_sha256'",
            ):
                tx.commit()
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10000)
            self.assertEqual((root / "decisions.jsonl").read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
