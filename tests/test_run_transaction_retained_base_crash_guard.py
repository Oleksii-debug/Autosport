from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction, RunTransactionError


class RunTransactionRetainedBaseCrashGuardTests(unittest.TestCase):
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
    def _prepared(cls, root: Path, *, run_id: str):
        registry = RunRegistry.initialize_pristine(root / "run_registry.json")
        book_path = root / "paper_book.json"
        PaperBook("1000").save(book_path)
        base_payload = book_path.read_bytes()
        base_book_sha = hashlib.sha256(base_payload).hexdigest()

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
        tx.run_ledger_path.write_bytes(b"")

        terminal = PaperBook.load(book_path)
        terminal.open_ticket(
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
            reason="retained BASE crash guard test",
            placed_at="2026-09-25T18:55:00+00:00",
            bankroll_id="paper-main",
            currency="EUR",
        )
        tx.stage_outputs(terminal, ledger_path)
        return tx, experiment_key, book_path, base_payload

    def test_declared_retained_contract_missing_base_sidecar_cannot_precommit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, experiment_key, _book_path, _base_payload = self._prepared(
                root,
                run_id="crash-between-manifest-and-base-sidecar",
            )

            # Model a hard process exit after the new-format manifest became durable
            # but before start() made the declared BASE sidecar durable.
            tx.base_book_snapshot_path.unlink()

            with self.assertRaisesRegex(
                RunTransactionError,
                "retained base PaperBook canonical file is missing",
            ):
                tx.precommit(self._summary(tx.run_id, experiment_key))

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "staging")
            self.assertEqual(manifest["new"], {})
            self.assertFalse(tx.terminal_book_snapshot_path.exists())

    def test_true_legacy_staging_backfills_exact_base_before_precommit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, experiment_key, _book_path, base_payload = self._prepared(
                root,
                run_id="legacy-staging-base-backfill",
            )

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            manifest.pop("retained", None)
            tx.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tx.base_book_snapshot_path.unlink()

            tx.precommit(self._summary(tx.run_id, experiment_key))

            after = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(after["phase"], "precommitted")
            self.assertEqual(
                after["retained"],
                {
                    "base_paper_book": "paper_book.base.json",
                    "terminal_paper_book": "paper_book.terminal.json",
                },
            )
            self.assertEqual(tx.base_book_snapshot_path.read_bytes(), base_payload)
            self.assertTrue(tx.terminal_book_snapshot_path.is_file())

    def test_base_sidecar_loss_after_precommit_blocks_economic_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx, experiment_key, book_path, base_payload = self._prepared(
                root,
                run_id="base-loss-after-precommit",
            )
            tx.precommit(self._summary(tx.run_id, experiment_key))
            tx.base_book_snapshot_path.unlink()

            with self.assertRaisesRegex(
                RunTransactionError,
                "retained base PaperBook canonical file is missing",
            ):
                tx.commit()

            # Commit preflight must fail before the canonical economic PaperBook is
            # replaced by staged NEW state.
            self.assertEqual(book_path.read_bytes(), base_payload)
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "precommitted")


if __name__ == "__main__":
    unittest.main()
