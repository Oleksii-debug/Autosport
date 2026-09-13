import json
import tempfile
import unittest
from pathlib import Path

from autosport.product_journey_audit import run_product_journey_audit


class ProductJourneyAuditTests(unittest.TestCase):
    def test_shipped_demo_executes_research_paper_settlement_evaluation_journey(self):
        root = Path(__file__).resolve().parents[1] / "examples" / "tt_demo"
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "product-journey-audit.json"
            exit_code = run_product_journey_audit(output, root, root / "research_plan.json")
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["canonical_strategy_id"], "research-replay-v1")
        self.assertEqual(payload["event_count"], 4)
        self.assertEqual(payload["settled_ticket_count"], 1)
        self.assertEqual(payload["balance"], "990")
        self.assertEqual(payload["net_profit"], "-10")
        self.assertTrue(payload["transaction_terminal"])
        self.assertFalse(payload["real_historical_point_in_time_market_coverage_verified"])
        self.assertFalse(payload["profitability_claim"])
        self.assertFalse(payload["real_money_execution"])
        self.assertFalse(payload["human_tested"])
        self.assertFalse(payload["nvda_verified"])

    def test_rejects_research_plan_outside_selected_dataset_directory(self):
        root = Path(__file__).resolve().parents[1] / "examples" / "tt_demo"
        with tempfile.TemporaryDirectory() as temp:
            temp_root = Path(temp)
            outside_plan = temp_root / "research_plan.json"
            outside_plan.write_bytes((root / "research_plan.json").read_bytes())
            output = temp_root / "audit.json"
            exit_code = run_product_journey_audit(output, root, outside_plan)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "FAIL")
        self.assertIn("contained in the selected dataset directory", payload["error"])
        self.assertFalse(payload["profitability_claim"])
        self.assertFalse(payload["real_money_execution"])
        self.assertFalse(payload["human_tested"])
        self.assertFalse(payload["nvda_verified"])


if __name__ == "__main__":
    unittest.main()
