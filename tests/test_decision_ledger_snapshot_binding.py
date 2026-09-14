import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.run_transaction import RunTransaction


class DecisionLedgerSnapshotBindingTests(unittest.TestCase):
    @staticmethod
    def _record(run_id: str, value: int) -> DecisionRecord:
        return DecisionRecord(
            run_id,
            "agent",
            "2026-01-01T00:00:00+00:00",
            "OBSERVE",
            {"value": value},
            f"ctx-{value}",
        )

    def test_stage_outputs_does_not_reopen_canonical_ledger_after_verified_identity(self):
        """A valid path replacement must not substitute bytes after BASE verification."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = PaperBook("10000")
            book_path = root / "paper_book.json"
            book.save(book_path)

            canonical = JsonlDecisionLedger(root / "decisions.jsonl")
            canonical.append(self._record("base-a", 1))
            verified_base_bytes = canonical.path.read_bytes()

            replacement = JsonlDecisionLedger(root / "replacement-valid.jsonl")
            replacement.append(self._record("base-b", 2))
            replacement_bytes = replacement.path.read_bytes()
            self.assertNotEqual(verified_base_bytes, replacement_bytes)
            replacement.verify_integrity()

            tx = RunTransaction.start(
                root,
                run_id="snapshot-swap",
                experiment_key="experiment",
                market_sha256="a" * 64,
                results_sha256="b" * 64,
                strategy_id="baseline-v1",
                base_paper_book_sha256=sha256_file(book_path),
                base_decision_ledger_sha256=sha256_file(canonical.path),
            )
            run_ledger = JsonlDecisionLedger(tx.run_ledger_path)
            run_ledger.append(self._record("snapshot-swap", 3))
            run_bytes = run_ledger.path.read_bytes()

            original_verify = RunTransaction._verify_decision_ledger
            swapped = False

            def verify_then_swap(path: Path, label: str) -> int:
                nonlocal swapped
                count = original_verify(path, label)
                if Path(path) == canonical.path and not swapped:
                    # Both A and B are individually valid ledgers. The attack is
                    # identity substitution between the old verify and combine reads,
                    # not malformed evidence.
                    canonical.path.write_bytes(replacement_bytes)
                    swapped = True
                return count

            with patch.object(
                RunTransaction,
                "_verify_decision_ledger",
                side_effect=verify_then_swap,
            ):
                tx.stage_outputs(book, canonical.path)

            # A read-once implementation may either fail closed before this point or
            # construct NEW from the immutable bytes whose BASE identity was verified.
            # It must never reopen the substituted live path and silently stage B.
            self.assertEqual(
                tx.staged_ledger_path.read_bytes(),
                verified_base_bytes + run_bytes,
            )


if __name__ == "__main__":
    unittest.main()
