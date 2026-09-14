import json
import tempfile
import unittest
from pathlib import Path

from autosport.restart_recovery_audit import run_restart_recovery_audit


class RestartRecoveryAuditTests(unittest.TestCase):
    def test_audit_proves_persistent_restart_recovery_and_bounded_endurance(self):
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

            self.assertEqual(payload["endurance_status"], "PASS")
            self.assertEqual(payload["endurance_history_events"], 20_000)
            self.assertEqual(payload["endurance_current_quotes"], 2_000)
            self.assertEqual(payload["endurance_accepted_duplicate_pass"], 0)
            self.assertTrue(payload["endurance_independent_reingest_hash_match"])
            self.assertEqual(
                payload["endurance_restart_hashes"],
                [payload["endurance_replay_dataset_hash"]] * 3,
            )
            self.assertEqual(payload["endurance_restart_projection_counts"], [2_000] * 3)
            self.assertEqual(payload["endurance_paper_tickets_settled_first_pass"], 50)
            self.assertEqual(payload["endurance_paper_tickets_settled_second_pass"], 0)
            self.assertTrue(payload["endurance_corrupt_health_rejected"])
            self.assertTrue(payload["endurance_corrupt_paper_book_rejected"])
            self.assertEqual(len(payload["endurance_stable_invariant_fingerprint"]), 64)

            self.assertFalse(payload["real_money_execution"])
            self.assertFalse(payload["human_tested"])
            self.assertFalse(payload["nvda_verified"])


if __name__ == "__main__":
    unittest.main()
