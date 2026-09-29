import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.domain import TicketLeg
from autosport.integrity import sha256_file
from autosport.paper import PaperBook
import autosport.run_transaction as run_transaction_module
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
        staged_book = PaperBook.load(book_path)
        staged_book.open_ticket(
            [
                TicketLeg(
                    event_id="canonical-target-fixture:event",
                    market_id="canonical-target-fixture:winner",
                    selection_id="canonical-target-fixture:home",
                    locked_odds=Decimal("2"),
                    sport="motorsport",
                    exchange_side="back",
                )
            ],
            Decimal("1"),
            reason="canonical-target-bound-new",
            placed_at="2000-01-01T00:00:00+00:00",
        )
        tx.stage_outputs(staged_book, ledger.path)
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

    def test_canonical_paperbook_semantics_are_checked_on_captured_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "paper_book.json"
            target.write_bytes(b"{}")

            with patch.object(
                PaperBook,
                "load",
                return_value=PaperBook("10000"),
            ) as hostile_path_load:
                with self.assertRaisesRegex(
                    RunTransactionError,
                    "canonical PaperBook semantic validation failed",
                ):
                    RunTransaction._verified_canonical_paper_book_snapshot(
                        target,
                        "PaperBook",
                    )

            hostile_path_load.assert_not_called()

    def test_canonical_paperbook_semantics_do_not_dispatch_mutable_load_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "paper_book.json"
            target.write_bytes(b"{}")

            with patch.object(
                PaperBook,
                "load_bytes",
                return_value=PaperBook("10000"),
            ) as hostile_load_bytes:
                with self.assertRaisesRegex(
                    RunTransactionError,
                    "canonical PaperBook semantic validation failed",
                ):
                    RunTransaction._verified_canonical_paper_book_snapshot(
                        target,
                        "PaperBook",
                    )

            hostile_load_bytes.assert_not_called()

    def test_canonical_snapshot_primary_open_does_not_follow_swap_to_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "canonical.json"
            external = root / "external.json"
            backup = root / "canonical.backup.json"
            target.write_bytes(b'{"canonical":true}')
            external.write_bytes(b'{"external":true}')
            canonical_open = run_transaction_module._open_read_only_descriptor
            calls = 0

            def swap_then_open(path: Path) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    target.replace(backup)
                    try:
                        target.symlink_to(external)
                    except (OSError, NotImplementedError) as exc:
                        backup.replace(target)
                        self.skipTest(
                            f"file symlink replacement is unavailable on this platform: {exc}"
                        )
                return canonical_open(path)

            with patch.object(
                run_transaction_module,
                "_open_read_only_descriptor",
                side_effect=swap_then_open,
            ):
                with self.assertRaisesRegex(
                    RunTransactionError,
                    "canonical file is unreadable|canonical path changed",
                ):
                    RunTransaction._read_canonical_file_snapshot(
                        target,
                        "fixture",
                    )

            self.assertEqual(calls, 1)
            self.assertEqual(external.read_bytes(), b'{"external":true}')

    def test_canonical_snapshot_verification_reopen_does_not_follow_swap_to_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "canonical.json"
            external = root / "external.json"
            backup = root / "canonical.backup.json"
            target.write_bytes(b'{"canonical":true}')
            external.write_bytes(b'{"external":true}')
            canonical_open = run_transaction_module._open_read_only_descriptor
            calls = 0

            def swap_on_verification_open(path: Path) -> int:
                nonlocal calls
                calls += 1
                if calls == 2:
                    target.replace(backup)
                    try:
                        target.symlink_to(external)
                    except (OSError, NotImplementedError) as exc:
                        backup.replace(target)
                        self.skipTest(
                            f"file symlink replacement is unavailable on this platform: {exc}"
                        )
                return canonical_open(path)

            with patch.object(
                run_transaction_module,
                "_open_read_only_descriptor",
                side_effect=swap_on_verification_open,
            ):
                with self.assertRaisesRegex(
                    RunTransactionError,
                    "canonical path must be a stable regular non-symlink file",
                ):
                    RunTransaction._read_canonical_file_snapshot(
                        target,
                        "fixture",
                    )

            self.assertEqual(calls, 2)
            self.assertEqual(external.read_bytes(), b'{"external":true}')


    def test_canonical_snapshot_verification_regular_replacement_ignores_hostile_sameopenfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "canonical.json"
            replacement = root / "replacement.json"
            backup = root / "canonical.backup.json"
            target.write_bytes(b'{"canonical":1}')
            replacement.write_bytes(b'{"replace___":1}')
            original_stat = os.stat(target, follow_symlinks=False)
            os.utime(
                replacement,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            canonical_open = run_transaction_module._open_read_only_descriptor
            hostile_sameopenfile_executed = False
            calls = 0

            def swap_on_verification_open(path: Path) -> int:
                nonlocal calls
                calls += 1
                if calls == 2:
                    target.replace(backup)
                    replacement.replace(target)
                return canonical_open(path)

            def hostile_sameopenfile(left: int, right: int) -> bool:
                del left, right
                nonlocal hostile_sameopenfile_executed
                hostile_sameopenfile_executed = True
                return True

            with patch.object(
                run_transaction_module,
                "_open_read_only_descriptor",
                side_effect=swap_on_verification_open,
            ), patch.object(
                run_transaction_module.os.path,
                "sameopenfile",
                side_effect=hostile_sameopenfile,
            ):
                with self.assertRaisesRegex(
                    RunTransactionError,
                    "canonical path must be a stable regular non-symlink file",
                ):
                    RunTransaction._read_canonical_file_snapshot(
                        target,
                        "fixture",
                    )

            self.assertEqual(calls, 2)
            self.assertFalse(hostile_sameopenfile_executed)
            self.assertEqual(backup.read_bytes(), b'{"canonical":1}')
            self.assertEqual(target.read_bytes(), b'{"replace___":1}')

    def test_canonical_snapshot_primary_open_binds_initial_regular_file_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "canonical.json"
            replacement = root / "replacement.json"
            backup = root / "canonical.backup.json"
            target.write_bytes(b'{"canonical":1}')
            replacement.write_bytes(b'{"replacement":1}')
            original_stat = os.stat(target, follow_symlinks=False)
            os.utime(
                replacement,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            canonical_open = run_transaction_module._open_read_only_descriptor
            hostile_samestat_executed = False
            calls = 0

            def swap_regular_then_open(path: Path) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    target.replace(backup)
                    replacement.replace(target)
                return canonical_open(path)

            def hostile_samestat(left: os.stat_result, right: os.stat_result) -> bool:
                del left, right
                nonlocal hostile_samestat_executed
                hostile_samestat_executed = True
                return True

            with patch.object(
                run_transaction_module,
                "_open_read_only_descriptor",
                side_effect=swap_regular_then_open,
            ), patch.object(
                run_transaction_module.os.path,
                "samestat",
                side_effect=hostile_samestat,
            ):
                with self.assertRaisesRegex(
                    RunTransactionError,
                    "canonical path must be a stable regular non-symlink file",
                ):
                    RunTransaction._read_canonical_file_snapshot(
                        target,
                        "fixture",
                    )

            self.assertEqual(calls, 1)
            self.assertFalse(hostile_samestat_executed)
            self.assertEqual(backup.read_bytes(), b'{"canonical":1}')
            self.assertEqual(target.read_bytes(), b'{"replacement":1}')

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
