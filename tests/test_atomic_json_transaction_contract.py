import json
import tempfile
import unittest
from pathlib import Path

from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.run_transaction import RunTransaction, RunTransactionError


class AtomicJsonTransactionContractTests(unittest.TestCase):
    def test_precommit_rejects_every_nonfinite_number_before_durable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            book_path = root / "paper_book.json"
            ledger_path = root / "decisions.jsonl"
            book = PaperBook("10000")
            book.save(book_path)
            ledger_path.write_bytes(b"")
            tx = RunTransaction.start(
                root,
                run_id="strict-summary-nonfinite-contract",
                experiment_key="experiment",
                market_sha256="a" * 64,
                results_sha256="b" * 64,
                strategy_id="baseline-v1",
                base_paper_book_sha256=sha256_file(book_path),
                base_decision_ledger_sha256=sha256_file(ledger_path),
            )
            tx.stage_outputs(book, ledger_path)

            for invalid, token in (
                (float("nan"), "NaN"),
                (float("inf"), "Infinity"),
                (float("-inf"), "-Infinity"),
            ):
                with self.subTest(token=token):
                    with self.assertRaisesRegex(
                        RunTransactionError,
                        rf"staged run summary contains non-finite JSON value '{token}'",
                    ):
                        tx.precommit({"nested": {"ambiguous": invalid}})

                    self.assertFalse(tx.staged_summary_path.exists())
                    manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
                    self.assertEqual(manifest["phase"], "staging")
                    self.assertEqual(manifest["new"], {})
                    self.assertEqual(PaperBook.load(book_path).balance, 10000)
                    self.assertEqual(ledger_path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
