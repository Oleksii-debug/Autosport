from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.run_transaction import RunTransaction
from autosport.session import AutosportSession
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
            "transaction_schema_version": RunTransaction.SCHEMA_VERSION,
            "transaction_run_id": run_id,
            "run_id": run_id,
            "paper_book_sha256": "5" * 64,
            "decision_ledger_sha256": "6" * 64,
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
            "balance": final_balance,
            "evaluation": {
                "initial_bankroll": "10000",
                "final_balance": final_balance,
                "committed_stake": "0",
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

    def test_loader_accepts_real_canonical_session_summary(self):
        dataset = load_dataset(Path(__file__).resolve().parents[1] / "examples" / "tt_demo")
        with tempfile.TemporaryDirectory() as temp:
            session = AutosportSession(temp, "10000")
            try:
                result = session.run_dataset(dataset)
            finally:
                session.close()
            payload = json.loads(Path(result.result_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["transaction_schema_version"], RunTransaction.SCHEMA_VERSION)
            evidence = load_strategy_run_summary(result.result_path)

        self.assertEqual(evidence.run_id, result.replay.run_id)
        self.assertEqual(evidence.strategy_id, "baseline-v1")
        self.assertEqual(evidence.canonical_strategy_id, "baseline-v1")

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
        self.assertTrue(report["truth"]["governed_historical_import"])
        self.assertFalse(report["truth"]["real_historical_market_coverage_verified"])
        self.assertFalse(report["truth"]["licensing_retention_verified"])

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
                    roi="0.002",
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

    def test_loader_rejects_noncanonical_transaction_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._summary(
                Path(temp) / "bad-schema.json",
                strategy_id="baseline-v1",
                canonical_strategy_id="baseline-v1",
                net_profit="0",
                roi="0",
                final_balance="10000",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["transaction_schema_version"] = RunTransaction.SCHEMA_VERSION + 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "canonical transaction schema"):
                load_strategy_run_summary(path)

    def test_loader_rejects_missing_durable_economic_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._summary(
                Path(temp) / "missing-hash.json",
                strategy_id="baseline-v1",
                canonical_strategy_id="baseline-v1",
                net_profit="0",
                roi="0",
                final_balance="10000",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("paper_book_sha256")
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "paper_book_sha256"):
                load_strategy_run_summary(path)

    def test_loader_rejects_evaluation_balance_divergence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._summary(
                Path(temp) / "bad-balance.json",
                strategy_id="baseline-v1",
                canonical_strategy_id="baseline-v1",
                net_profit="25",
                roi="0.05",
                final_balance="10025",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["evaluation"]["final_balance"] = "999999"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "final_balance does not match canonical run balance"):
                load_strategy_run_summary(path)

    def test_loader_rejects_profit_identity_divergence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._summary(
                Path(temp) / "bad-profit.json",
                strategy_id="baseline-v1",
                canonical_strategy_id="baseline-v1",
                net_profit="25",
                roi="0.05",
                final_balance="10025",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["evaluation"]["net_profit"] = "40"
            payload["evaluation"]["roi"] = "0.08"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "committed_stake"):
                load_strategy_run_summary(path)

    def test_loader_rejects_roi_inconsistent_with_profit_and_settled_stake(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._summary(
                Path(temp) / "bad-roi.json",
                strategy_id="baseline-v1",
                canonical_strategy_id="baseline-v1",
                net_profit="25",
                roi="0.05",
                final_balance="10025",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["evaluation"]["roi"] = "99"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "roi does not match net_profit / settled_stake"):
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
