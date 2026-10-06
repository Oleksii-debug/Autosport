from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper import PaperBook
from autosport.paper_campaign_admission import PaperCampaignAdmissionError
from autosport.paper_execution_reality import PaperAttemptOutcome, PaperExecutionLedger
from paper_campaign_admission_test_support import AdmissionFixture


class PaperCampaignAdmissionTests(unittest.TestCase):
    def test_admission_consumes_existing_execution_ticket_and_restart_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            root = fixture.workspace
            before = PaperBook.load(root / "paper_book.json")
            self.assertEqual(len(before.tickets), 1)
            self.assertEqual(JsonlDecisionLedger(root / "decisions.jsonl").verify_integrity(), 1)

            first = fixture.admit(fixture.coordinator())
            self.assertEqual(first.ticket_id, fixture.execution_ticket_id)
            self.assertEqual(len(PaperBook.load(root / "paper_book.json").tickets), 1)
            self.assertEqual(JsonlDecisionLedger(root / "decisions.jsonl").verify_integrity(), 2)
            self.assertEqual(
                AgentLoopRuntime(root / "agent-loop.json").snapshot().phase,
                AgentLoopPhase.WAIT_OUTCOME,
            )

            second = fixture.admit(fixture.coordinator(resumed=True))
            self.assertEqual(first, second)
            self.assertEqual(len(PaperBook.load(root / "paper_book.json").tickets), 1)
            self.assertEqual(JsonlDecisionLedger(root / "decisions.jsonl").verify_integrity(), 2)

    def test_partial_moved_execution_truth_is_admitted_without_fabricating_pre_execution_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(
                Path(directory),
                outcome=PaperAttemptOutcome.PARTIAL,
                execution_odds="2.40",
                execution_stake="4.00",
            )
            ticket = PaperBook.load(fixture.workspace / "paper_book.json").tickets[
                fixture.execution_ticket_id
            ]
            self.assertEqual(ticket.stake, Decimal("4.00"))
            self.assertEqual(ticket.legs[0].locked_odds, Decimal("2.40"))

            receipt = fixture.admit(fixture.coordinator())
            self.assertEqual(receipt.ticket_id, ticket.ticket_id)
            self.assertEqual(len(PaperBook.load(fixture.workspace / "paper_book.json").tickets), 1)

    def test_rejected_and_unknown_execution_are_not_admissible_and_create_no_exposure(self) -> None:
        for outcome in (PaperAttemptOutcome.REJECTED, PaperAttemptOutcome.UNKNOWN):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                fixture = AdmissionFixture(Path(directory), outcome=outcome)
                self.assertEqual(PaperBook.load(fixture.workspace / "paper_book.json").tickets, {})
                with self.assertRaisesRegex(
                    PaperCampaignAdmissionError,
                    "REJECTED/UNKNOWN",
                ):
                    fixture.admit(fixture.coordinator())
                self.assertEqual(
                    JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verify_integrity(),
                    1,
                )
                self.assertEqual(PaperBook.load(fixture.workspace / "paper_book.json").tickets, {})

    def test_execution_identity_is_bound_into_economic_decision_and_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            receipt = fixture.admit(fixture.coordinator())
            decision = JsonlDecisionLedger(
                fixture.workspace / "decisions.jsonl"
            ).verified_economic_decision(
                receipt.decision_id,
                fixture.goal,
                risk_policy=fixture.risk,
            )
            self.assertEqual(
                decision.payload["paper_execution_run_id"], fixture.execution_run_id
            )
            self.assertEqual(
                decision.payload["paper_execution_attempt_id"], fixture.execution_attempt_id
            )
            self.assertEqual(
                decision.payload["paper_execution_ticket_id"], fixture.execution_ticket_id
            )
            bridge_state = json.loads(
                (fixture.workspace / "paper-learning-bridge.json").read_text(encoding="utf-8")
            )
            binding = bridge_state["bindings"][fixture.execution_ticket_id]
            parameters = dict(binding["action_parameters"])
            self.assertEqual(parameters["paper_execution_run_id"], fixture.execution_run_id)
            self.assertEqual(
                parameters["paper_execution_attempt_id"], fixture.execution_attempt_id
            )

    def test_execution_authority_fields_cannot_be_overridden_by_caller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            with self.assertRaisesRegex(PaperCampaignAdmissionError, "replace PAPER execution"):
                fixture.admit(
                    fixture.coordinator(),
                    decision_payload={"paper_execution_run_id": "forged-run"},
                )
            with self.assertRaisesRegex(PaperCampaignAdmissionError, "replace PAPER execution"):
                fixture.admit(
                    fixture.coordinator(),
                    action_parameters=(("paper_execution_attempt_id", "forged-attempt"),),
                )

    def test_completed_run_without_preexisting_decision_authority_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory), seed_execution_decision=False)
            with self.assertRaisesRegex(
                PaperCampaignAdmissionError,
                "pre-execution decision-origin",
            ):
                fixture.admit(fixture.coordinator())
            self.assertEqual(
                JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verify_integrity(),
                0,
            )

    def test_caller_cannot_select_another_execution_decision_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            with self.assertRaisesRegex(
                PaperCampaignAdmissionError,
                "caller execution_decision_id conflicts",
            ):
                fixture.admit(
                    fixture.coordinator(),
                    execution_decision_id="forged-decision",
                )
            self.assertEqual(
                JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verify_integrity(),
                1,
            )

    def test_durable_decision_with_wrong_plan_fingerprint_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(
                Path(directory),
                decision_plan_fingerprint="0" * 64,
            )
            with self.assertRaisesRegex(
                PaperCampaignAdmissionError,
                "canonical execution authority",
            ):
                fixture.admit(fixture.coordinator())
            self.assertEqual(
                JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verify_integrity(),
                1,
            )

    def test_execution_ledger_subclass_cannot_spoof_authority(self) -> None:
        class SpoofingLedger(PaperExecutionLedger):
            events_called = False

            def events(self, run_id: str):
                type(self).events_called = True
                return super().events(run_id)

        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            spoof = SpoofingLedger(fixture.execution_ledger_path)
            with self.assertRaisesRegex(TypeError, "exact PaperExecutionLedger"):
                fixture.coordinator(execution_ledger=spoof)
            self.assertFalse(SpoofingLedger.events_called)

    def test_decision_ledger_subclass_cannot_spoof_execution_origin(self) -> None:
        class SpoofingDecisionLedger(JsonlDecisionLedger):
            verified_records_called = False

            def verified_records(self, *args, **kwargs):
                type(self).verified_records_called = True
                return super().verified_records(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            spoof = SpoofingDecisionLedger(fixture.workspace / "decisions.jsonl")
            with self.assertRaisesRegex(TypeError, "exact JsonlDecisionLedger"):
                fixture.coordinator(decision_ledger=spoof)
            self.assertFalse(SpoofingDecisionLedger.verified_records_called)


if __name__ == "__main__":
    unittest.main()
