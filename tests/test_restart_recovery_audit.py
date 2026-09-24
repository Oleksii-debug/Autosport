import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.endurance import EnduranceConfig
from autosport import restart_recovery_audit as restart_audit


class _BrokenStringError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("diagnostic rendering must not escape")


class _BrokenNameMeta(type):
    def __getattribute__(cls, name):
        if name == "__name__":
            raise RuntimeError("diagnostic type metadata must not escape")
        return super().__getattribute__(name)


class _BrokenMetadataError(Exception, metaclass=_BrokenNameMeta):
    def __str__(self) -> str:
        raise RuntimeError("diagnostic rendering must not escape")


class RestartRecoveryAuditTests(unittest.TestCase):
    @staticmethod
    def _restart_stub() -> dict[str, object]:
        return {
            "status": "PASS",
            "balance": "101",
            "ticket_count": 1,
            "paper_book_sha256": "a" * 64,
            "decision_ledger_sha256": "b" * 64,
        }

    @staticmethod
    def _recovery_stub() -> dict[str, object]:
        return {
            "status": "PASS",
            "disposition": "aborted_uncommitted",
            "corrupt_manifest_rejected": True,
            "corrupt_manifest_base_unchanged": True,
            "corrupt_manifest_registry_unresolved": True,
        }

    @staticmethod
    def _endurance_stub() -> dict[str, object]:
        replay_hash = "c" * 64
        return {
            "status": "PASS",
            "history_events": 20_000,
            "current_quotes": 2_000,
            "accepted_duplicate_pass": 0,
            "replay_dataset_hash": replay_hash,
            "restart_hashes": [replay_hash] * 3,
            "restart_projection_counts": [2_000] * 3,
            "independent_reingest_hash_match": True,
            "paper_tickets_settled_first_pass": 50,
            "paper_tickets_settled_second_pass": 0,
            "corrupt_health_rejected": True,
            "corrupt_paper_book_rejected": True,
            "stable_invariant_fingerprint": "d" * 64,
            "real_money_execution": False,
        }

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
            self.assertTrue(payload["transaction_corrupt_manifest_rejected"])
            self.assertTrue(payload["transaction_corrupt_manifest_base_unchanged"])
            self.assertTrue(payload["transaction_corrupt_manifest_registry_unresolved"])
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

    def test_semantic_failure_with_broken_stringification_still_publishes_fail_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "restart-recovery-audit.json"
            output.write_text('{"status":"LAST_KNOWN"}\n', encoding="utf-8")

            with patch.object(
                restart_audit,
                "_audit_session_restart",
                side_effect=_BrokenStringError(),
            ):
                self.assertEqual(restart_audit.run_restart_recovery_audit(output), 1)

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(
                payload["error"],
                "_BrokenStringError: exception details unavailable",
            )
            self.assertFalse(payload["real_money_execution"])
            self.assertFalse(payload["human_tested"])
            self.assertFalse(payload["nvda_verified"])

    def test_semantic_failure_with_hostile_type_metadata_still_publishes_fail_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "restart-recovery-audit.json"
            output.write_text('{"status":"LAST_KNOWN"}\n', encoding="utf-8")

            with patch.object(
                restart_audit,
                "_audit_session_restart",
                side_effect=_BrokenMetadataError(),
            ):
                self.assertEqual(restart_audit.run_restart_recovery_audit(output), 1)

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(
                payload["error"],
                "_BrokenMetadataError: exception details unavailable",
            )
            self.assertFalse(payload["real_money_execution"])
            self.assertFalse(payload["human_tested"])
            self.assertFalse(payload["nvda_verified"])

    def test_evidence_publication_replaces_hardlink_without_mutating_external_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = root / "external-sentinel.json"
            output = root / "restart-recovery-audit.json"
            sentinel = b"external-sentinel-must-not-change\n"
            external.write_bytes(sentinel)
            try:
                os.link(external, output)
            except OSError as exc:
                self.skipTest(f"hard links unavailable: {exc}")

            with (
                patch.object(restart_audit, "_audit_session_restart", return_value=self._restart_stub()),
                patch.object(restart_audit, "_audit_uncommitted_recovery", return_value=self._recovery_stub()),
                patch.object(restart_audit, "_audit_bounded_endurance", return_value=self._endurance_stub()),
            ):
                self.assertEqual(restart_audit.run_restart_recovery_audit(output), 0)

            self.assertEqual(external.read_bytes(), sentinel)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["recovery_disposition"], "aborted_uncommitted")
            self.assertTrue(payload["transaction_corrupt_manifest_rejected"])
            self.assertTrue(payload["transaction_corrupt_manifest_base_unchanged"])
            self.assertTrue(payload["transaction_corrupt_manifest_registry_unresolved"])

    def test_replace_failure_preserves_previous_evidence_and_cleans_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "restart-recovery-audit.json"
            previous = b'{"status":"LAST_KNOWN"}\n'
            output.write_bytes(previous)

            with (
                patch.object(restart_audit, "_audit_session_restart", return_value=self._restart_stub()),
                patch.object(restart_audit, "_audit_uncommitted_recovery", return_value=self._recovery_stub()),
                patch.object(restart_audit, "_audit_bounded_endurance", return_value=self._endurance_stub()),
                patch("autosport.integrity.os.replace", side_effect=OSError("injected publication failure")),
            ):
                with self.assertRaisesRegex(OSError, "injected publication failure"):
                    restart_audit.run_restart_recovery_audit(output)

            self.assertEqual(output.read_bytes(), previous)
            self.assertEqual(
                {item.name for item in root.iterdir()},
                {output.name, f".{output.name}.lock"},
            )


if __name__ == "__main__":
    unittest.main()
