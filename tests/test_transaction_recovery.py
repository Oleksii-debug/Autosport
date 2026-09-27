import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport._paperbook_preload_authority_guard as paper_guard
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

            # Simulate process death after the canonical PaperBook PREPARE + replace
            # but before COMMIT. Path-bound authority recovery must complete that exact
            # generation before the remaining transaction artifacts are reconciled.
            canonical_book_path = root / "paper_book.json"
            staged_sha = sha256_file(tx.staged_book_path)
            _records, committed, pending = paper_guard._read_witnesses(canonical_book_path)
            self.assertIsNotNone(committed)
            self.assertIsNone(pending)
            generation = committed[0] + 1
            paper_guard._append_witness(
                canonical_book_path,
                event=paper_guard._PREPARE,
                generation=generation,
                snapshot_sha256=staged_sha,
            )
            os.replace(tx.staged_book_path, canonical_book_path)
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
                "canonical Decision Ledger verification copy integrity validation failed",
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
                "canonical Decision Ledger verification copy integrity validation failed",
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
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            book_path = root / "paper_book.json"
            PaperBook("10000").save(book_path)
            ledger_path = self._write_corrupt_ledger(root)
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

    def test_stage_outputs_rejects_unbound_paper_book_before_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = root / "paper_book.json"
            ledger_path = root / "decisions.jsonl"
            PaperBook("10000").save(book_path)
            ledger_path.write_bytes(b"")
            canonical_bytes = book_path.read_bytes()
            witness_path = paper_guard._witness_path(book_path)
            witness_bytes = witness_path.read_bytes()

            tx = RunTransaction.start(
                root,
                run_id="reject-unbound-stage",
                experiment_key="experiment",
                market_sha256="a" * 64,
                results_sha256="b" * 64,
                strategy_id="baseline-v1",
                base_paper_book_sha256=sha256_file(book_path),
                base_decision_ledger_sha256=sha256_file(ledger_path),
            )
            arbitrary = PaperBook("25000")

            with self.assertRaisesRegex(
                RunTransactionError,
                "verified path-bound authority",
            ):
                tx.stage_outputs(arbitrary, ledger_path)

            self.assertFalse(tx.staged_book_path.exists())
            self.assertFalse(paper_guard._witness_path(tx.staged_book_path).exists())
            self.assertEqual(book_path.read_bytes(), canonical_bytes)
            self.assertEqual(witness_path.read_bytes(), witness_bytes)

    def test_stage_outputs_binding_ignores_rebound_guard_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = root / "paper_book.json"
            ledger_path = root / "decisions.jsonl"
            PaperBook("10000").save(book_path)
            ledger_path.write_bytes(b"")
            snapshot_before = book_path.read_bytes()
            witness_path = paper_guard._witness_path(book_path)
            witness_before = witness_path.read_bytes()
            tx = RunTransaction.start(
                root,
                run_id="sealed-binding-reject-unbound",
                experiment_key="experiment",
                market_sha256="a" * 64,
                results_sha256="b" * 64,
                strategy_id="baseline-v1",
                base_paper_book_sha256=sha256_file(book_path),
                base_decision_ledger_sha256=sha256_file(ledger_path),
            )
            arbitrary = PaperBook("25000")
            hostile_calls: list[tuple[object, object]] = []

            def hostile(book: object, path: object) -> None:
                hostile_calls.append((book, path))
                return None

            with patch.object(paper_guard, "_require_bound_book", new=hostile):
                with self.assertRaisesRegex(
                    RunTransactionError,
                    "verified path-bound authority",
                ):
                    tx.stage_outputs(arbitrary, ledger_path)

            self.assertEqual(hostile_calls, [])
            self.assertFalse(tx.staged_book_path.exists())
            self.assertFalse(paper_guard._witness_path(tx.staged_book_path).exists())
            self.assertEqual(book_path.read_bytes(), snapshot_before)
            self.assertEqual(witness_path.read_bytes(), witness_before)

    def test_stage_outputs_bound_book_survives_rebound_guard_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = root / "paper_book.json"
            ledger_path = root / "decisions.jsonl"
            PaperBook("10000").save(book_path)
            ledger_path.write_bytes(b"")
            working = PaperBook.load(book_path)
            tx = RunTransaction.start(
                root,
                run_id="sealed-binding-positive",
                experiment_key="experiment",
                market_sha256="a" * 64,
                results_sha256="b" * 64,
                strategy_id="baseline-v1",
                base_paper_book_sha256=sha256_file(book_path),
                base_decision_ledger_sha256=sha256_file(ledger_path),
            )
            hostile_calls: list[tuple[object, object]] = []

            def hostile(book: object, path: object) -> None:
                hostile_calls.append((book, path))
                raise AssertionError("rebound mutable binding validator was dispatched")

            with patch.object(paper_guard, "_require_bound_book", new=hostile):
                staged_book_hash, _staged_ledger_hash = tx.stage_outputs(
                    working,
                    ledger_path,
                )

            self.assertEqual(hostile_calls, [])
            self.assertEqual(staged_book_hash, sha256_file(book_path))
            self.assertTrue(tx.staged_book_path.is_file())
            self.assertFalse(paper_guard._witness_path(tx.staged_book_path).exists())

    def test_stage_outputs_keeps_transaction_snapshot_non_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = root / "paper_book.json"
            ledger_path = root / "decisions.jsonl"
            initial = PaperBook("10000")
            initial.save(book_path)
            ledger_path.write_bytes(b"")
            working = PaperBook.load(book_path)

            tx = RunTransaction.start(
                root,
                run_id="bounded-stage",
                experiment_key="experiment",
                market_sha256="a" * 64,
                results_sha256="b" * 64,
                strategy_id="baseline-v1",
                base_paper_book_sha256=sha256_file(book_path),
                base_decision_ledger_sha256=sha256_file(ledger_path),
            )
            tx.stage_outputs(working, ledger_path)

            self.assertTrue(tx.staged_book_path.is_file())
            self.assertFalse(paper_guard._witness_path(tx.staged_book_path).exists())
            structural = PaperBook.load_bytes(tx.staged_book_path.read_bytes())
            self.assertEqual(structural.balance, 10000)
            with self.assertRaisesRegex(
                ValueError,
                "missing independent durable opening witness",
            ):
                PaperBook.load(tx.staged_book_path)
            self.assertEqual(PaperBook.load(book_path).balance, 10000)

    def test_precommitted_noop_paper_book_recovery_preserves_witness_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = root / "paper_book.json"
            ledger_path = root / "decisions.jsonl"
            PaperBook("10000").save(book_path)
            ledger_path.write_bytes(b"")

            base_book_hash = sha256_file(book_path)
            base_ledger_hash = sha256_file(ledger_path)
            canonical_book_bytes = book_path.read_bytes()
            witness_path = paper_guard._witness_path(book_path)
            witness_bytes = witness_path.read_bytes()
            _records_before, committed_before, pending_before = paper_guard._read_witnesses(
                book_path
            )
            self.assertIsNotNone(committed_before)
            self.assertIsNone(pending_before)

            run_id = "noop-paper-book-recovery"
            market_hash = "a" * 64
            results_hash = "b" * 64
            strategy_id = "baseline-v1"
            tx = RunTransaction.start(
                root,
                run_id=run_id,
                experiment_key="experiment",
                market_sha256=market_hash,
                results_sha256=results_hash,
                strategy_id=strategy_id,
                base_paper_book_sha256=base_book_hash,
                base_decision_ledger_sha256=base_ledger_hash,
            )
            JsonlDecisionLedger(tx.run_ledger_path).append(
                DecisionRecord(
                    run_id,
                    "agent",
                    "2026-01-01T00:00:00+00:00",
                    "OBSERVE",
                    {"reason": "no-bet"},
                    "ctx",
                )
            )

            working = PaperBook.load(book_path)
            staged_book_hash, staged_ledger_hash = tx.stage_outputs(
                working,
                ledger_path,
            )
            self.assertEqual(staged_book_hash, base_book_hash)
            self.assertNotEqual(staged_ledger_hash, base_ledger_hash)
            self.assertFalse(paper_guard._witness_path(tx.staged_book_path).exists())

            summary = tx.precommit(
                {
                    "run_id": run_id,
                    "experiment_key": "experiment",
                    "market_sha256": market_hash,
                    "sealed_results_sha256": results_hash,
                    "strategy_id": strategy_id,
                    "real_money_execution": False,
                }
            )
            self.assertEqual(summary["paper_book_sha256"], base_book_hash)
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["base"]["paper_book_sha256"], base_book_hash)
            self.assertEqual(manifest["new"]["paper_book_sha256"], base_book_hash)
            self.assertNotEqual(
                manifest["new"]["decision_ledger_sha256"],
                base_ledger_hash,
            )

            registry_item = {
                "run_id": run_id,
                "market_sha256": market_hash,
                "results_sha256": results_hash,
                "strategy_id": strategy_id,
                "base_paper_book_sha256": base_book_hash,
                "base_decision_ledger_sha256": base_ledger_hash,
            }
            recovery = RunTransaction.recover(
                root,
                run_id=run_id,
                registry_item=registry_item,
                experiment_key="experiment",
            )
            self.assertEqual(recovery.disposition, "committed")
            self.assertIsNotNone(recovery.summary_path)
            self.assertTrue(recovery.summary_path.is_file())

            _records_after, committed_after, pending_after = paper_guard._read_witnesses(
                book_path
            )
            self.assertEqual(book_path.read_bytes(), canonical_book_bytes)
            self.assertEqual(witness_path.read_bytes(), witness_bytes)
            self.assertEqual(committed_after, committed_before)
            self.assertIsNone(pending_after)
            self.assertFalse(paper_guard._witness_path(tx.staged_book_path).exists())
            self.assertEqual(PaperBook.load(book_path).balance, 10000)
            self.assertEqual(
                len(ledger_path.read_text(encoding="utf-8").splitlines()),
                1,
            )
            committed_manifest = json.loads(
                tx.manifest_path.read_text(encoding="utf-8")
            )
            self.assertEqual(committed_manifest["phase"], "canonical_committed")

            # Recovery is idempotent and still must not create an economic generation
            # solely because the other transaction artifacts are already committed.
            retry = RunTransaction.recover(
                root,
                run_id=run_id,
                registry_item=registry_item,
                experiment_key="experiment",
            )
            self.assertEqual(retry.disposition, "committed")
            self.assertEqual(witness_path.read_bytes(), witness_bytes)
            _records_retry, committed_retry, pending_retry = paper_guard._read_witnesses(
                book_path
            )
            self.assertEqual(committed_retry, committed_before)
            self.assertIsNone(pending_retry)


    def test_precommit_rejects_nonfinite_summary_before_manifest_precommit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = root / "paper_book.json"
            ledger_path = root / "decisions.jsonl"
            book = PaperBook("10000")
            book.save(book_path)
            ledger_path.write_bytes(b"")
            tx = RunTransaction.start(
                root,
                run_id="strict-summary-precommit",
                experiment_key="experiment",
                market_sha256="a" * 64,
                results_sha256="b" * 64,
                strategy_id="baseline-v1",
                base_paper_book_sha256=sha256_file(book_path),
                base_decision_ledger_sha256=sha256_file(ledger_path),
            )
            tx.stage_outputs(book, ledger_path)

            with self.assertRaisesRegex(
                RunTransactionError,
                "staged run summary contains non-finite JSON value 'NaN'",
            ):
                tx.precommit({"ambiguous": float("nan")})

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "staging")
            self.assertEqual(manifest["new"], {})
            self.assertEqual(PaperBook.load(book_path).balance, 10000)
            self.assertEqual(ledger_path.read_text(encoding="utf-8"), "")

    def test_commit_rejects_boolean_summary_schema_before_economic_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, _item = self._precommitted_transaction(root)
            summary = json.loads(tx.staged_summary_path.read_text(encoding="utf-8"))
            summary["transaction_schema_version"] = True
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
                "transaction binding mismatch: transaction_schema_version",
            ):
                tx.commit()
            self.assertEqual(PaperBook.load(root / "paper_book.json").balance, 10000)
            self.assertEqual((root / "decisions.jsonl").read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
