import json
import tempfile
import unittest
from pathlib import Path

from autosport.research_demo_audit import run_research_demo_audit


class ResearchDemoAuditTests(unittest.TestCase):
    def test_shipped_research_demo_passes_with_truth_boundaries(self):
        root = Path(__file__).resolve().parents[1] / "examples" / "tt_demo"
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "research-demo-audit.json"
            self.assertEqual(run_research_demo_audit(output, root), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["strategy_id"], "research-replay-v1")
        self.assertEqual(payload["replay_event_count"], 4)
        self.assertEqual(payload["settled_ticket_count"], 1)
        self.assertEqual(payload["persistent_ticket_count"], 1)
        self.assertEqual(payload["persistent_balance"], "990")
        self.assertEqual(payload["paper_net_profit"], "-10")
        self.assertTrue(payload["sample_fixture"])
        self.assertFalse(payload["real_historical_market_proof"])
        self.assertFalse(payload["profitability_claim"])
        self.assertFalse(payload["real_money_execution"])
        self.assertFalse(payload["human_tested"])
        self.assertFalse(payload["nvda_verified"])

    def test_missing_demo_fails_closed_without_truth_promotion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "research-demo-audit.json"
            missing = root / "missing-demo"
            self.assertEqual(run_research_demo_audit(output, missing), 1)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "FAIL")
        self.assertTrue(payload["sample_fixture"])
        self.assertFalse(payload["real_historical_market_proof"])
        self.assertFalse(payload["profitability_claim"])
        self.assertFalse(payload["real_money_execution"])
        self.assertFalse(payload["human_tested"])
        self.assertFalse(payload["nvda_verified"])


if __name__ == "__main__":
    unittest.main()
