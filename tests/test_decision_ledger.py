import json
import tempfile
import unittest
from pathlib import Path

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger


class DecisionLedgerTests(unittest.TestCase):
    def test_append_only_record_is_hashed_and_has_no_outcome_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            digest = ledger.append(DecisionRecord("run-1", "agent", "2026-01-01T00:00:00+00:00", "OBSERVE", {"x": 1}, "ctx"))
            envelope = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(envelope["sha256"], digest)
            self.assertNotIn("result", envelope["record"])
            self.assertNotIn("outcome", envelope["record"])


if __name__ == "__main__":
    unittest.main()
