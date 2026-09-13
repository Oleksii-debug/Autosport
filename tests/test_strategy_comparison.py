from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autosport.strategy_comparison import (
    compare_strategy_runs,
    load_strategy_run_summary,
    main,
)


class StrategyComparisonTests(unittest.TestCase):
    def _summary(
        self,
        path: Path,
        *,
        strategy_id: str,
        canonical_strategy_id: str,
        net_profit: str,
        roi: str,
        final_balance: str,
        market_sha256: str = "1" * 64,
        run_id: str = "run-1",
    ) -> Path:
        payload = {
            "schema_version": 2,
            "transaction_schema_version": 2,
            "transaction_run_id": run_id,
            "run_id": run_id,
            "dataset_name": "licensed-table-tennis-slice",
            "sport": "table_tennis",
            "dataset_schema_version": 2,
            "historical_import_identity": "3" * 64,
            "market_sha256": market_sha256,
            "sealed_results_sha256": "2" * 64,
            "replay_dataset_hash": "4" * 64,
            "event_count": 100,
            "strategy_id": strategy_id,
            "strategy_runtime": {
                "strategy_id": strategy_id,
                "canonical_strategy_id": canonical_strategy_id,
                "label": strategy_id,
                "agent_names": ["TestAgent"],
                "opens_paper_tickets": True,
                "research_plan_sha256": None,
            },
            "evaluation": {
                "initial_bankroll": "10000",
                "final_balance": final_balance,
                "settled_stake": "500",
                "net_profit": net_profit,
                "roi": roi,
                "won": 3,
                "lost": 2,
                "void": 0,
            },
            "real_money_execution": False,
        }
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def test_same_dataset_strategies_compare_with_truth_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            baseline_path = self._summary(
                root / "baseline.json",
                strategy_id="baseline-v1",
                canonical_strategy_id="baseline-v1",
                net_profit="25",
                roi="0.05",
                final_balance="10025",
                run_id="baseline-run",
            )
            research_path = self._summary(
                root / "research.json",
                strategy_id="research-replay-v1:abc",
                canonical_strategy_id="research-replay-v1",
                net_profit="40",
                roi="0.08",
                final_balance="10040",
                run_id="research-run",
            )
            report = compare_strategy_runs(
                (
                    load_strategy_run_summary(research_path),
                    load_strategy_run_summary(baseline_path),
                )
            )

        self.assertEqual(report["baseline_strategy_id"], "baseline-v1")
        self.assertEqual(
            report["observed_paper_order_by_net_profit"],
            ["research-replay-v1:abc", "baseline-v1"],
        )
        entries = {item["strategy_id"]: item for item in report["strategies"]}
        self.assertEqual(entries["research-replay-v1:abc"]["observed_delta_vs_baseline"]["net_profit"], "15")
        self.assertFalse(report["truth"]["profitability_claim"])
        self.assertFalse(report["truth"]["predictive_superiority_claim"])
        self.assertFalse(report["truth"]["out_of_sample_claim"])
        self.assertFalse(report["truth"]["real_money_execution"])
        self.assertTrue(report["truth"]["historical_proof"])

    def test_mismatched_dataset_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = load_strategy_run_summary(
                self._summary(
                    root / "a.json",
                    strategy_id="baseline-v1",
                    canonical_strategy_id="baseline-v1",
                    net_profit="0",
                    roi="0",
                    final_balance="10000",
                )
            )
            second = load_strategy_run_summary(
                self._summary(
                    root / "b.json",
                    strategy_id="observe-only-v1",
                    canonical_strategy_id="observe-only-v1",
                    net_profit="0",
                    roi="0",
                    final_balance="10000",
                    market_sha256="9" * 64,
                    run_id="run-2",
                )
            )
            with self.assertRaisesRegex(ValueError, "identical sealed dataset"):
                compare_strategy_runs((first, second))

    def test_duplicate_strategy_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = load_strategy_run_summary(
                self._summary(
                    root / "a.json",
                    strategy_id="baseline-v1",
                    canonical_strategy_id="baseline-v1",
                    net_profit="0",
                    roi="0",
                    final_balance="10000",
                    run_id="run-a",
                )
            )
            second = load_strategy_run_summary(
                self._summary(
                    root / "b.json",
                    strategy_id="baseline-v1",
                    canonical_strategy_id="baseline-v1",
                    net_profit="1",
                    roi="0.01",
                    final_balance="10001",
                    run_id="run-b",
                )
            )
            with self.assertRaisesRegex(ValueError, "duplicate strategy_id"):
                compare_strategy_runs((first, second))

    def test_loader_rejects_real_money_truth_violation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._summary(
                Path(temp) / "bad.json",
                strategy_id="baseline-v1",
                canonical_strategy_id="baseline-v1",
                net_profit="0",
                roi="0",
                final_balance="10000",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["real_money_execution"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "real_money_execution"):
                load_strategy_run_summary(path)

    def test_module_cli_writes_machine_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a = self._summary(
                root / "a.json",
                strategy_id="baseline-v1",
                canonical_strategy_id="baseline-v1",
                net_profit="0",
                roi="0",
                final_balance="10000",
                run_id="run-a",
            )
            b = self._summary(
                root / "b.json",
                strategy_id="observe-only-v1",
                canonical_strategy_id="observe-only-v1",
                net_profit="0",
                roi="0",
                final_balance="10000",
                run_id="run-b",
            )
            output = root / "comparison.json"
            rc = main([str(a), str(b), "--output", str(output)])
            self.assertEqual(rc, 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "autosport_strategy_comparison")
            self.assertFalse(report["truth"]["profitability_claim"])


if __name__ == "__main__":
    unittest.main()
