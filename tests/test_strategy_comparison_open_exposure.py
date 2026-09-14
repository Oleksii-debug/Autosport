from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autosport.run_transaction import RunTransaction
from autosport.strategies import strategy_spec
from autosport.strategy_comparison import load_strategy_run_summary


class StrategyComparisonOpenExposureTests(unittest.TestCase):
    def test_loader_uses_explicit_committed_stake_for_open_exposure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "open-exposure.json"
            payload = {
                "schema_version": 2,
                "transaction_schema_version": RunTransaction.SCHEMA_VERSION,
                "transaction_run_id": "run-open",
                "run_id": "run-open",
                "paper_book_sha256": "5" * 64,
                "decision_ledger_sha256": "6" * 64,
                "dataset_name": "governed-table-tennis-slice",
                "sport": "table_tennis",
                "dataset_schema_version": 2,
                "historical_import_identity": "3" * 64,
                "market_sha256": "1" * 64,
                "sealed_results_sha256": "2" * 64,
                "replay_dataset_hash": "4" * 64,
                "event_count": 1,
                "strategy_id": "baseline-v1",
                "strategy_runtime": {
                    "strategy_id": "baseline-v1",
                    "canonical_strategy_id": "baseline-v1",
                    "label": "baseline-v1",
                    "agent_names": list(strategy_spec("baseline-v1").agent_names),
                    "opens_paper_tickets": True,
                    "research_plan_sha256": None,
                },
                # balance=9900, committed_stake=100, initial=10000 => net_profit=0.
                "balance": "9900",
                "evaluation": {
                    "initial_bankroll": "10000",
                    "final_balance": "9900",
                    "committed_stake": "100",
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

            evidence = load_strategy_run_summary(path)

        self.assertEqual(str(evidence.final_balance), "9900")
        self.assertEqual(str(evidence.committed_stake), "100")
        self.assertEqual(str(evidence.net_profit), "0")
        self.assertEqual(str(evidence.roi), "0")

    def test_loader_rejects_missing_committed_stake(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing-committed.json"
            payload = {
                "schema_version": 2,
                "transaction_schema_version": RunTransaction.SCHEMA_VERSION,
                "transaction_run_id": "run-missing",
                "run_id": "run-missing",
                "paper_book_sha256": "5" * 64,
                "decision_ledger_sha256": "6" * 64,
                "dataset_name": "governed-table-tennis-slice",
                "sport": "table_tennis",
                "dataset_schema_version": 2,
                "historical_import_identity": "3" * 64,
                "market_sha256": "1" * 64,
                "sealed_results_sha256": "2" * 64,
                "replay_dataset_hash": "4" * 64,
                "event_count": 1,
                "strategy_id": "baseline-v1",
                "strategy_runtime": {
                    "strategy_id": "baseline-v1",
                    "canonical_strategy_id": "baseline-v1",
                    "label": "baseline-v1",
                    "agent_names": list(strategy_spec("baseline-v1").agent_names),
                    "opens_paper_tickets": True,
                    "research_plan_sha256": None,
                },
                "balance": "10000",
                "evaluation": {
                    "initial_bankroll": "10000",
                    "final_balance": "10000",
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

            with self.assertRaisesRegex(ValueError, "committed_stake"):
                load_strategy_run_summary(path)


if __name__ == "__main__":
    unittest.main()
