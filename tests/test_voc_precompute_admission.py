from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.voc_evaluation import VOCEvaluationError
from autosport.voc_outcome_scoring import (
    append_paired_voc_admission,
    append_paired_voc_terminal,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
T_DECISION = "2026-09-20T00:00:00Z"
T_DEADLINE = "2026-09-20T00:00:10Z"
T_TIMEOUT = "2026-09-20T00:00:11Z"


class VOCPrecomputeAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "decision-ledger.jsonl"
        self.ledger = JsonlDecisionLedger(self.path)
        self.scope = {
            "sport_id": "table_tennis",
            "league_id": "league-voc",
            "regime_id": "regime-voc",
            "urgency_id": "normal",
            "contradiction_state": "none",
        }
        self.baseline = {
            "candidate_id": "baseline",
            "backend_id": "local",
            "model_id": "baseline-model",
            "config_sha256": SHA_B,
        }
        self.challenger = {
            "candidate_id": "challenger",
            "backend_id": "cloud",
            "model_id": "challenger-model",
            "config_sha256": SHA_C,
        }
        context = DecisionRecord(
            replay_run_id="replay-context",
            agent="voc-admission-test",
            observed_ts=T_DECISION,
            action="VOC_ROUTE_CONTEXT",
            payload={
                "voc_current_context": {
                    "request_id": "request-1",
                    "decision_input_sha256": SHA_A,
                    "task_class": "route-voc",
                    **self.scope,
                }
            },
            context_hash=SHA_A,
            decision_id="decision-context",
            recorded_at=T_DECISION,
        )
        self.context_sha = self.ledger.append(context)

    def _admit(self) -> str:
        return append_paired_voc_admission(
            self.ledger,
            admission_id="admission-1",
            decision_context_sha256=self.context_sha,
            decision_input_sha256=SHA_A,
            decision_deadline=T_DEADLINE,
            research_protocol_id="protocol-1",
            cohort_id="cohort-1",
            task_class="route-voc",
            scope=self.scope,
            baseline_compute_identity=self.baseline,
            challenger_compute_identity=self.challenger,
            replay_run_id="replay-admission",
            agent="voc-admission-test",
            recorded_at=T_DECISION,
        )

    def test_admission_is_preoutput_and_terminal_survives_restart(self) -> None:
        admission_sha = self._admit()
        terminal_sha = append_paired_voc_terminal(
            self.ledger,
            admission_sha256=admission_sha,
            status="timeout",
            observed_extra_compute_cost=Decimal("0.25"),
            observed_extra_latency_seconds=Decimal("10"),
            replay_run_id="replay-terminal",
            agent="voc-admission-test",
            recorded_at=T_TIMEOUT,
        )

        records = JsonlDecisionLedger(self.path).verified_records()
        self.assertEqual([record.action for record in records], [
            "VOC_ROUTE_CONTEXT",
            "VOC_PAIRED_ADMISSION",
            "VOC_PAIRED_TERMINAL",
        ])
        admission = records[1].payload["voc_paired_admission"]
        self.assertEqual(admission["decision_context_sha256"], self.context_sha)
        self.assertEqual(admission["baseline_compute_identity"], self.baseline)
        self.assertEqual(admission["challenger_compute_identity"], self.challenger)
        self.assertEqual(admission["research_protocol_id"], "protocol-1")
        self.assertEqual(admission["cohort_id"], "cohort-1")
        self.assertNotIn("baseline_output_sha256", admission)
        self.assertNotIn("challenger_output_sha256", admission)
        self.assertNotIn("baseline_action", admission)
        self.assertNotIn("challenger_action", admission)

        terminal = records[2].payload["voc_terminal"]
        self.assertEqual(terminal["admission_sha256"], admission_sha)
        self.assertEqual(terminal["status"], "timeout")
        self.assertEqual(terminal["observed_extra_compute_cost"], "0.25")
        self.assertEqual(terminal["observed_extra_latency_seconds"], "10")
        self.assertEqual(
            terminal_sha,
            __import__("hashlib").sha256(
                __import__("json").dumps(
                    records[2].to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
        )

    def test_timeout_cannot_be_backdated_before_frozen_deadline(self) -> None:
        admission_sha = self._admit()
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "recorded before the frozen deadline",
        ):
            append_paired_voc_terminal(
                self.ledger,
                admission_sha256=admission_sha,
                status="timeout",
                observed_extra_compute_cost=Decimal("0.25"),
                observed_extra_latency_seconds=Decimal("1"),
                replay_run_id="replay-terminal-early",
                agent="voc-admission-test",
                recorded_at="2026-09-20T00:00:05Z",
            )


if __name__ == "__main__":
    unittest.main()
