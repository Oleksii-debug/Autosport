import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.research_demo_audit as research_demo_audit
from autosport.integrity import atomic_write_json as real_atomic_write_json
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

    def test_machine_evidence_publication_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "research-demo-audit.json"
            missing = root / "missing-demo"
            original = '{"status":"PREVIOUS"}\n'
            output.write_text(original, encoding="utf-8")

            def reject_nonfinite(path, payload):
                real_atomic_write_json(path, {**payload, "probe": float("nan")})

            with patch.object(
                research_demo_audit,
                "atomic_write_json",
                side_effect=reject_nonfinite,
            ):
                with self.assertRaises(ValueError):
                    run_research_demo_audit(output, missing)

            self.assertEqual(output.read_text(encoding="utf-8"), original)
            self.assertEqual(list(output.parent.glob(f".{output.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
