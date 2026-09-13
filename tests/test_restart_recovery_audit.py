import json
import tempfile
import unittest
from pathlib import Path

from autosport.restart_recovery_audit import run_restart_recovery_audit


class RestartRecoveryAuditTests(unittest.TestCase):
    def test_audit_proves_persistent_restart_and_fail_closed_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "restart-recovery-audit.json"
            self.assertEqual(run_restart_recovery_audit(output), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["session_restart_status"], "PASS")
            self.assertEqual(payload["transaction_recovery_status"], "PASS")
            self.assertEqual(payload["recovery_disposition"], "aborted_uncommitted")
            self.assertGreater(payload["ticket_count"], 0)
            self.assertEqual(len(payload["paper_book_sha256"]), 64)
            self.assertEqual(len(payload["decision_ledger_sha256"]), 64)
            self.assertFalse(payload["real_money_execution"])
            self.assertFalse(payload["human_tested"])
            self.assertFalse(payload["nvda_verified"])


if __name__ == "__main__":
    unittest.main()
