import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction, RunTransactionError


class RunTransactionTerminalPaperBookSnapshotTests(unittest.TestCase):
    @staticmethod
    def _prepare(root: Path, *, run_id: str):
        registry = RunRegistry.initialize_pristine(root / "run_registry.json")

        book_path = root / "paper_book.json"
        PaperBook("1000").save(book_path)
        base_book_payload = book_path.read_bytes()
        base_book_sha = hashlib.sha256(base_book_payload).hexdigest()

        ledger_path = root / "decisions.jsonl"
        ledger_path.write_bytes(b"")
        base_ledger_sha = hashlib.sha256(b"").hexdigest()

        experiment_key = registry.begin(
            "a" * 64,
            "b" * 64,
            "baseline-v1",
            run_id,
            base_paper_book_sha256=base_book_sha,
            base_decision_ledger_sha256=base_ledger_sha,
        )
        tx = RunTransaction.start(
            root,
            run_id=run_id,
            experiment_key=experiment_key,
            market_sha256="a" * 64,
            results_sha256="b" * 64,
            strategy_id="baseline-v1",
            base_paper_book_sha256=base_book_sha,
            base_decision_ledger_sha256=base_ledger_sha,
        )

        # A run-local empty ledger is a valid exact suffix for this focused
        # PaperBook evidence test.
        tx.run_ledger_path.write_bytes(b"")

        terminal_book = PaperBook.load(book_path)
        terminal_book.open_ticket(
            [
                TicketLeg(
                    event_id="event-1",
                    market_id="market-1",
                    selection_id="selection-1",
                    locked_odds=Decimal("2.0"),
                    sport="soccer",
                )
            ],
            Decimal("125"),
            reason="terminal snapshot evidence test",
            placed_at="2026-09-23T20:00:00+00:00",
            bankroll_id="paper-main",
            currency="EUR",
        )
        new_book_sha, new_ledger_sha = tx.stage_outputs(
            terminal_book,
            ledger_path,
        )
        return (
            registry,
            tx,
            experiment_key,
            book_path,
            terminal_book,
            new_book_sha,
            new_ledger_sha,
        )

    @staticmethod
    def _summary(run_id: str, experiment_key: str) -> dict:
        return {
            "schema_version": 2,
            "run_id": run_id,
            "experiment_key": experiment_key,
            "market_sha256": "a" * 64,
            "sealed_results_sha256": "b" * 64,
            "strategy_id": "baseline-v1",
            "real_money_execution": False,
        }

    @classmethod
    def _complete(cls, root: Path, *, run_id: str):
        (
            registry,
            tx,
            experiment_key,
            book_path,
            terminal_book,
            new_book_sha,
            new_ledger_sha,
        ) = cls._prepare(root, run_id=run_id)

        tx.precommit(cls._summary(run_id, experiment_key))
        expected_terminal = tx.terminal_book_snapshot_path.read_bytes()
        self_hash = hashlib.sha256(expected_terminal).hexdigest()
        if self_hash != new_book_sha:
            raise AssertionError("test setup terminal sidecar does not match staged NEW")

        summary_path = tx.commit()
        registry.complete(
            experiment_key,
            str(summary_path),
            paper_book_sha256=new_book_sha,
            decision_ledger_sha256=new_ledger_sha,
        )
        tx.mark_registry_completed()
        return (
            tx,
            book_path,
            terminal_book,
            expected_terminal,
            new_book_sha,
        )

    def test_terminal_snapshot_survives_later_canonical_book_generation_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                _tx,
                book_path,
                terminal_book,
                expected_terminal,
                new_book_sha,
            ) = self._complete(root, run_id="terminal-retained")

            self.assertEqual(terminal_book.balance, Decimal("875"))
            self.assertEqual(
                hashlib.sha256(expected_terminal).hexdigest(),
                new_book_sha,
            )

            # A later run may legitimately advance the shared canonical PaperBook.
            # Historical run evidence must remain the exact committed NEW bytes.
            PaperBook("4321").save(book_path)

            detached = RunTransaction(root, "terminal-retained")
            snapshot = detached.verified_terminal_paper_book_snapshot()

            self.assertEqual(snapshot.payload, expected_terminal)
            self.assertEqual(snapshot.sha256, new_book_sha)
            self.assertNotEqual(book_path.read_bytes(), expected_terminal)

    def test_terminal_snapshot_tamper_fails_against_completed_registry_new_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, _book_path, _terminal_book, _expected, _new_sha = self._complete(
                root,
                run_id="terminal-tamper",
            )

            PaperBook("777").save(tx.terminal_book_snapshot_path)

            detached = RunTransaction(root, "terminal-tamper")
            with self.assertRaisesRegex(
                RunTransactionError,
                "retained terminal PaperBook SHA-256 does not match transaction NEW",
            ):
                detached.verified_terminal_paper_book_snapshot()

    def test_terminal_readback_rejects_in_progress_registry_even_after_precommit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                _registry,
                tx,
                experiment_key,
                _book_path,
                _terminal_book,
                _new_book_sha,
                _new_ledger_sha,
            ) = self._prepare(root, run_id="terminal-not-completed")

            tx.precommit(self._summary("terminal-not-completed", experiment_key))
            tx.commit()

            detached = RunTransaction(root, "terminal-not-completed")
            with self.assertRaisesRegex(
                RunTransactionError,
                "requires a completed registry identity",
            ):
                detached.verified_terminal_paper_book_snapshot()

    def test_terminal_retention_failure_cannot_advance_precommit_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                _registry,
                tx,
                experiment_key,
                _book_path,
                _terminal_book,
                _new_book_sha,
                _new_ledger_sha,
            ) = self._prepare(root, run_id="terminal-retention-failure")

            original_write = RunTransaction._atomic_write_bytes

            def fail_terminal(path: Path, payload: bytes) -> None:
                if path == tx.terminal_book_snapshot_path:
                    raise OSError("simulated terminal retention failure")
                original_write(path, payload)

            with patch.object(
                RunTransaction,
                "_atomic_write_bytes",
                side_effect=fail_terminal,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "simulated terminal retention failure",
                ):
                    tx.precommit(
                        self._summary(
                            "terminal-retention-failure",
                            experiment_key,
                        )
                    )

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "staging")
            self.assertEqual(manifest["new"], {})
            self.assertFalse(tx.terminal_book_snapshot_path.exists())

    def test_legacy_precommitted_without_terminal_contract_keeps_recovery_but_no_positive_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                registry,
                tx,
                experiment_key,
                _book_path,
                _terminal_book,
                new_book_sha,
                new_ledger_sha,
            ) = self._prepare(root, run_id="legacy-precommitted")

            tx.precommit(self._summary("legacy-precommitted", experiment_key))

            # Model a transaction that had already crossed precommit before the
            # terminal-retention feature existed. Ordinary economic recovery remains
            # compatible, but this legacy state cannot mint terminal risk evidence.
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            manifest.pop("retained", None)
            tx.manifest_path.write_text(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            tx.terminal_book_snapshot_path.unlink()

            summary_path = tx.commit()
            registry.complete(
                experiment_key,
                str(summary_path),
                paper_book_sha256=new_book_sha,
                decision_ledger_sha256=new_ledger_sha,
            )
            tx.mark_registry_completed()

            detached = RunTransaction(root, "legacy-precommitted")
            with self.assertRaisesRegex(
                RunTransactionError,
                "lacks retained terminal PaperBook evidence contract",
            ):
                detached.verified_terminal_paper_book_snapshot()


if __name__ == "__main__":
    unittest.main()
