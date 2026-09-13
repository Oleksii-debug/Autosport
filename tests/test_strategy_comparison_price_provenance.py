from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autosport.run_transaction import RunTransaction
from autosport.strategy_comparison import compare_strategy_runs, load_strategy_run_summary


class StrategyComparisonPriceProvenanceTests(unittest.TestCase):
    @staticmethod
    def _summary(path: Path, *, strategy_id: str, source_id: str, run_id: str) -> Path:
        payload = {
            "schema_version": 2,
            "transaction_schema_version": RunTransaction.SCHEMA_VERSION,
            "transaction_run_id": run_id,
            "run_id": run_id,
            "paper_book_sha256": "5" * 64,
            "decision_ledger_sha256": "6" * 64,
            "dataset_name": "same-sealed-dataset",
            "sport": "table_tennis",
            "dataset_schema_version": 2,
            "historical_import_identity": "3" * 64,
            "market_sha256": "1" * 64,
            "sealed_results_sha256": "2" * 64,
            "replay_dataset_hash": "4" * 64,
            "event_count": 100,
            "strategy_id": strategy_id,
            "strategy_runtime": {
                "strategy_id": strategy_id,
                "canonical_strategy_id": strategy_id,
                "research_plan_sha256": None,
            },
            "market_price_truth": {
                "price_semantics": "betfair_available_to_back",
                "executable_quote_verified": True,
                "paper_fill_fidelity_verified": False,
                "source_ids": [source_id],
            },
            "balance": "10000",
            "evaluation": {
                "initial_bankroll": "10000",
                "final_balance": "10000",
                "committed_stake": "0",
                "settled_stake": "0",
                "net_profit": "0",
                "roi": "0",
                "won": 0,
                "lost": 0,
                "void": 0,
            },
            "real_money_execution": False,
        }
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def test_schema_v2_summaries_cannot_compare_with_unbound_price_source_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = self._summary(
                root / "first.json",
                strategy_id="baseline-v1",
                source_id="betfair_exchange_historical",
                run_id="run-a",
            )
            second_path = self._summary(
                root / "second.json",
                strategy_id="observe-only-v1",
                source_id="forged-provider",
                run_id="run-b",
            )

            with self.assertRaisesRegex(ValueError, "requires dataset_governance.source_ids"):
                compare_strategy_runs(
                    (
                        load_strategy_run_summary(first_path),
                        load_strategy_run_summary(second_path),
                    )
                )


if __name__ == "__main__":
    unittest.main()
