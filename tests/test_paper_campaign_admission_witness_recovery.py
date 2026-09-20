from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.paper_campaign_admission as admission_module
from autosport.agent_loop import AgentLoopRuntime
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper import PaperBook
from autosport.paper_campaign_admission import PaperCampaignAdmissionError
from paper_campaign_admission_test_support import AdmissionFixture


class PaperCampaignAdmissionWitnessRecoveryTests(unittest.TestCase):
    @staticmethod
    def _partial_candidate_then_error(path: Path, payload: str) -> None:
        prefix = payload[: max(1, len(payload) // 2)]
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(prefix)
            handle.flush()
            os.fsync(handle.fileno())
        raise OSError("simulated short witness candidate write")

    def test_prepared_witness_before_local_replace_recovers_without_second_execution_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            coordinator = fixture.coordinator()
            with patch.object(
                admission_module,
                "atomic_write_json",
                side_effect=RuntimeError("crash after PREPARED witness fsync"),
            ):
                with self.assertRaisesRegex(RuntimeError, "PREPARED witness"):
                    fixture.admit(coordinator)

            self.assertEqual(len(PaperBook.load(fixture.workspace / "paper_book.json").tickets), 1)
            self.assertEqual(
                JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verify_integrity(),
                1,
            )
            receipt = fixture.admit(fixture.coordinator(resumed=True))
            self.assertEqual(receipt.ticket_id, fixture.execution_ticket_id)
            self.assertEqual(len(PaperBook.load(fixture.workspace / "paper_book.json").tickets), 1)

    def test_partial_prepared_witness_candidate_never_corrupts_live_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            coordinator = fixture.coordinator()
            witness_before = coordinator._witness_path.read_bytes()
            with patch.object(
                coordinator,
                "_write_witness_candidate",
                side_effect=self._partial_candidate_then_error,
            ):
                with self.assertRaisesRegex(PaperCampaignAdmissionError, "durability barrier failed"):
                    fixture.admit(coordinator)
            self.assertEqual(coordinator._witness_path.read_bytes(), witness_before)
            receipt = fixture.admit(fixture.coordinator(resumed=True))
            self.assertEqual(receipt.ticket_id, fixture.execution_ticket_id)

    def test_committed_witness_before_local_replace_recovers_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            coordinator = fixture.coordinator()
            original_atomic_write = admission_module.atomic_write_json

            def fail_committed_local_write(path, value):
                admissions = value.get("admissions", {}) if isinstance(value, dict) else {}
                if any(
                    isinstance(record, dict) and record.get("phase") == "COMMITTED"
                    for record in admissions.values()
                ):
                    raise RuntimeError("crash after COMMITTED witness fsync")
                return original_atomic_write(path, value)

            with patch.object(
                admission_module,
                "atomic_write_json",
                side_effect=fail_committed_local_write,
            ):
                with self.assertRaisesRegex(RuntimeError, "COMMITTED witness"):
                    fixture.admit(coordinator)

            durable_action_id = AgentLoopRuntime(
                fixture.workspace / "agent-loop.json"
            ).snapshot().action_id
            self.assertIsNotNone(durable_action_id)
            self.assertEqual(len(PaperBook.load(fixture.workspace / "paper_book.json").tickets), 1)
            self.assertEqual(
                JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verify_integrity(),
                2,
            )

            receipt = fixture.admit(fixture.coordinator(resumed=True))
            self.assertEqual(receipt.action_id, durable_action_id)
            self.assertEqual(receipt.ticket_id, fixture.execution_ticket_id)
            self.assertEqual(len(PaperBook.load(fixture.workspace / "paper_book.json").tickets), 1)

    def test_complete_witness_corruption_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            coordinator = fixture.coordinator()
            fixture.admit(coordinator)
            lines = coordinator._witness_path.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["state_sha256"] = "0" * 64
            lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
            coordinator._witness_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(PaperCampaignAdmissionError, "witness digest mismatch"):
                fixture.coordinator(resumed=True)


if __name__ == "__main__":
    unittest.main()
