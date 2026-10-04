import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import TicketLeg
from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction, RunTransactionError


class RunTransactionTerminalCompletionTests(unittest.TestCase):
    @staticmethod
    def _prepare_canonical_commit(
        root: Path,
        sampling_draw_admission_receipt_sha256: str | None = None,
        summary_overrides: dict[str, object] | None = None,
    ):
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
            sampling_draw_admission_receipt_sha256=(
                sampling_draw_admission_receipt_sha256
            ),
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
            sampling_draw_admission_receipt_sha256=(
                sampling_draw_admission_receipt_sha256
            ),
        )
        staged_book = PaperBook.load(book_path)
        staged_book.open_ticket(
            [
                TicketLeg(
                    event_id="terminal-completion-fixture:event",
                    market_id="terminal-completion-fixture:winner",
                    selection_id="terminal-completion-fixture:home",
                    locked_odds=Decimal("2"),
                    sport="motorsport",
                    exchange_side="back",
                )
            ],
            Decimal("1"),
            reason="terminal-completion-bound-new",
            placed_at="2000-01-01T00:00:00+00:00",
        )
        tx.stage_outputs(staged_book, ledger.path)
        summary_payload = {
            "schema_version": 2,
            "run_id": run_id,
            "experiment_key": experiment_key,
            "market_sha256": market_sha256,
            "sealed_results_sha256": results_sha256,
            "strategy_id": strategy_id,
            "real_money_execution": False,
        }
        if summary_overrides:
            summary_payload.update(summary_overrides)
        summary = tx.precommit(summary_payload)
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

    def test_draw_admission_binding_survives_terminal_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            admission = "d" * 64
            tx, registry, key, summary, summary_path = self._prepare_canonical_commit(
                root,
                admission,
            )

            self.assertEqual(
                registry.get(key)["sampling_draw_admission_receipt_sha256"],
                admission,
            )
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["sampling_draw_admission_receipt_sha256"],
                admission,
            )
            self.assertEqual(
                summary["sampling_draw_admission_receipt_sha256"],
                admission,
            )

            registry.reconcile_completed_summary(
                key,
                summary_path,
                root / "paper_book.json",
            )
            detached = RunTransaction(root, tx.run_id)
            detached.mark_registry_completed()
            verified, _sha = registry.verified_completed_summary_for_run(tx.run_id)
            self.assertEqual(
                verified["sampling_draw_admission_receipt_sha256"],
                admission,
            )

    def test_replay_payload_evidence_survives_terminal_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            replay_evidence = {
                "event_count": 3,
                "replay_input_event_payload_sequence_sha256": "1" * 64,
                "replay_consumed_event_payload_sequence_sha256": "2" * 64,
                "replay_applied_event_payload_sequence_sha256": "3" * 64,
                "replay_consumed_event_payload_multiset_sha256": "4" * 64,
            }
            tx, registry, key, summary, summary_path = self._prepare_canonical_commit(
                root,
                summary_overrides=replay_evidence,
            )
            registry.reconcile_completed_summary(
                key,
                summary_path,
                root / "paper_book.json",
            )
            RunTransaction(root, tx.run_id).mark_registry_completed()
            verified, _sha = registry.verified_completed_summary_for_run(tx.run_id)

            for field_name, expected in replay_evidence.items():
                self.assertEqual(summary[field_name], expected)
                self.assertEqual(verified[field_name], expected)

    def test_precommit_rejects_partial_replay_payload_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                RunTransactionError,
                "replay_event_payload_evidence",
            ):
                self._prepare_canonical_commit(
                    Path(tmp),
                    summary_overrides={
                        "event_count": 1,
                        "replay_input_event_payload_sequence_sha256": "1" * 64,
                    },
                )

    def test_precommit_rejects_noncanonical_replay_payload_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                RunTransactionError,
                "replay_input_event_payload_sequence_sha256",
            ):
                self._prepare_canonical_commit(
                    Path(tmp),
                    summary_overrides={
                        "event_count": 1,
                        "replay_input_event_payload_sequence_sha256": "A" * 64,
                        "replay_consumed_event_payload_sequence_sha256": "2" * 64,
                        "replay_applied_event_payload_sequence_sha256": "3" * 64,
                        "replay_consumed_event_payload_multiset_sha256": "4" * 64,
                    },
                )

    def test_precommit_rejects_boolean_replay_event_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                RunTransactionError,
                "event_count",
            ):
                self._prepare_canonical_commit(
                    Path(tmp),
                    summary_overrides={
                        "event_count": True,
                        "replay_input_event_payload_sequence_sha256": "1" * 64,
                        "replay_consumed_event_payload_sequence_sha256": "2" * 64,
                        "replay_applied_event_payload_sequence_sha256": "3" * 64,
                        "replay_consumed_event_payload_multiset_sha256": "4" * 64,
                    },
                )

    def test_transaction_start_rejects_draw_admission_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            book_path = root / "paper_book.json"
            PaperBook("10000").save(book_path)
            ledger = JsonlDecisionLedger(root / "decisions.jsonl")
            ledger.path.touch()

            market_sha256 = "a" * 64
            results_sha256 = "b" * 64
            strategy_id = "baseline-v1"
            run_id = "draw-admission-mismatch"
            base_book_hash = sha256_file(book_path)
            base_ledger_hash = sha256_file(ledger.path)
            key = registry.begin(
                market_sha256,
                results_sha256,
                strategy_id,
                run_id,
                base_paper_book_sha256=base_book_hash,
                base_decision_ledger_sha256=base_ledger_hash,
                sampling_draw_admission_receipt_sha256="d" * 64,
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "sampling draw-admission binding mismatch",
            ):
                RunTransaction.start(
                    root,
                    run_id=run_id,
                    experiment_key=key,
                    market_sha256=market_sha256,
                    results_sha256=results_sha256,
                    strategy_id=strategy_id,
                    base_paper_book_sha256=base_book_hash,
                    base_decision_ledger_sha256=base_ledger_hash,
                    sampling_draw_admission_receipt_sha256="e" * 64,
                )

            self.assertFalse(
                (root / RunTransaction.ROOT_NAME / run_id).exists()
            )

    def test_transaction_start_rejects_omitted_bound_draw_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            book_path = root / "paper_book.json"
            PaperBook("10000").save(book_path)
            ledger = JsonlDecisionLedger(root / "decisions.jsonl")
            ledger.path.touch()

            market_sha256 = "a" * 64
            results_sha256 = "b" * 64
            strategy_id = "baseline-v1"
            run_id = "draw-admission-omitted"
            base_book_hash = sha256_file(book_path)
            base_ledger_hash = sha256_file(ledger.path)
            key = registry.begin(
                market_sha256,
                results_sha256,
                strategy_id,
                run_id,
                base_paper_book_sha256=base_book_hash,
                base_decision_ledger_sha256=base_ledger_hash,
                sampling_draw_admission_receipt_sha256="d" * 64,
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "sampling draw-admission binding mismatch",
            ):
                RunTransaction.start(
                    root,
                    run_id=run_id,
                    experiment_key=key,
                    market_sha256=market_sha256,
                    results_sha256=results_sha256,
                    strategy_id=strategy_id,
                    base_paper_book_sha256=base_book_hash,
                    base_decision_ledger_sha256=base_ledger_hash,
                )

            self.assertFalse(
                (root / RunTransaction.ROOT_NAME / run_id).exists()
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
