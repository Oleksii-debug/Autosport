import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.endurance import EnduranceConfig
from autosport import restart_recovery_audit as restart_audit


class RestartRecoveryAuditTests(unittest.TestCase):
    def test_packaged_endurance_profile_remains_release_scale(self):
        config = restart_audit._PACKAGED_ENDURANCE_CONFIG
        self.assertEqual(config.event_count, 20_000)
        self.assertEqual(config.quote_keys, 2_000)
        self.assertEqual(config.batch_size, 500)
        self.assertEqual(config.restart_cycles, 3)
        self.assertEqual(config.paper_tickets, 50)
        self.assertEqual(config.source_id, "packaged-restart-endurance-audit")

    def test_audit_proves_persistent_restart_recovery_and_bounded_endurance(self):
        unit_config = EnduranceConfig(
            event_count=200,
            quote_keys=20,
            batch_size=50,
            restart_cycles=2,
            paper_tickets=5,
            source_id="unit-restart-endurance-audit",
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            restart_audit,
            "_PACKAGED_ENDURANCE_CONFIG",
            unit_config,
        ):
            output = Path(tmp) / "restart-recovery-audit.json"
            self.assertEqual(restart_audit.run_restart_recovery_audit(output), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["session_restart_status"], "PASS")
            self.assertEqual(payload["transaction_recovery_status"], "PASS")
            self.assertEqual(payload["recovery_disposition"], "aborted_uncommitted")
            self.assertGreater(payload["ticket_count"], 0)
            self.assertEqual(len(payload["paper_book_sha256"]), 64)
            self.assertEqual(len(payload["decision_ledger_sha256"]), 64)

            self.assertEqual(payload["endurance_status"], "PASS")
            self.assertEqual(payload["endurance_history_events"], unit_config.event_count)
            self.assertEqual(payload["endurance_current_quotes"], unit_config.quote_keys)
            self.assertEqual(payload["endurance_accepted_duplicate_pass"], 0)
            self.assertTrue(payload["endurance_independent_reingest_hash_match"])
            self.assertEqual(
                payload["endurance_restart_hashes"],
                [payload["endurance_replay_dataset_hash"]] * unit_config.restart_cycles,
            )
            self.assertEqual(
                payload["endurance_restart_projection_counts"],
                [unit_config.quote_keys] * unit_config.restart_cycles,
            )
            self.assertEqual(
                payload["endurance_paper_tickets_settled_first_pass"],
                unit_config.paper_tickets,
            )
            self.assertEqual(payload["endurance_paper_tickets_settled_second_pass"], 0)
            self.assertTrue(payload["endurance_corrupt_health_rejected"])
            self.assertTrue(payload["endurance_corrupt_paper_book_rejected"])
            self.assertEqual(len(payload["endurance_stable_invariant_fingerprint"]), 64)

            self.assertFalse(payload["real_money_execution"])
            self.assertFalse(payload["human_tested"])
            self.assertFalse(payload["nvda_verified"])


if __name__ == "__main__":
    unittest.main()
