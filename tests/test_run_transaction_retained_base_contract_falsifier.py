import json
import tempfile
import unittest
from pathlib import Path

from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction, RunTransactionError


class RunTransactionRetainedBaseContractFalsifierTests(unittest.TestCase):
    @staticmethod
    def _start(root: Path, run_id: str) -> RunTransaction:
        RunRegistry.initialize_pristine(root / "run_registry.json")
        book_path = root / "paper_book.json"
        PaperBook("10000").save(book_path)
        base_sha = sha256_file(book_path)
        registry = RunRegistry.initialize_pristine(root / "run_registry.json")
        experiment_key = registry.begin(
            "a" * 64,
            "b" * 64,
            "baseline-v1",
            run_id,
            base_paper_book_sha256=base_sha,
            base_decision_ledger_sha256="c" * 64,
        )
        return RunTransaction.start(
            root,
            run_id=run_id,
            experiment_key=experiment_key,
            market_sha256="a" * 64,
            results_sha256="b" * 64,
            strategy_id="baseline-v1",
            base_paper_book_sha256=base_sha,
            base_decision_ledger_sha256="c" * 64,
        )

    def test_detached_base_readback_rejects_posthoc_matching_sidecar_without_retained_contract(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = self._start(root, "legacy-posthoc-base")
            exact_base_bytes = tx.base_book_snapshot_path.read_bytes()

            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["retained"],
                {
                    "base_paper_book": "paper_book.base.json",
                    "terminal_paper_book": "paper_book.terminal.json",
                },
            )

            # Model a genuinely historical manifest from before retained BASE evidence
            # existed, then create an exact matching sidecar only after the fact.
            del manifest["retained"]
            tx.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tx.base_book_snapshot_path.unlink()
            tx.base_book_snapshot_path.write_bytes(exact_base_bytes)

            detached = RunTransaction(root, "legacy-posthoc-base")
            with self.assertRaises(RunTransactionError):
                detached.verified_base_paper_book_snapshot()


if __name__ == "__main__":
    unittest.main()
