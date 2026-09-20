from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper import PaperBook
from autosport.paper_campaign_admission import PaperCampaignAdmissionError
from autosport.paper_campaign_runtime import PaperCampaignRuntime
from paper_campaign_admission_test_support import AdmissionFixture


class PaperCampaignAdmissionCommittedBindingTests(unittest.TestCase):
    def test_attempt_ticket_or_execution_decision_substitution_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            coordinator = fixture.coordinator()
            fixture.admit(coordinator)
            baseline_count = JsonlDecisionLedger(
                fixture.workspace / "decisions.jsonl"
            ).verify_integrity()

            for overrides, expected in (
                ({"execution_attempt_id": "paper-attempt-v2-forged"}, "missing or duplicated"),
                ({"execution_ticket_id": "forged-ticket"}, "exactly one canonical PaperTicket"),
                (
                    {"execution_decision_id": "forged-decision"},
                    "caller execution_decision_id conflicts",
                ),
            ):
                with self.subTest(overrides=overrides):
                    with self.assertRaisesRegex(PaperCampaignAdmissionError, expected):
                        fixture.admit(fixture.coordinator(resumed=True), **overrides)
                    self.assertEqual(
                        JsonlDecisionLedger(
                            fixture.workspace / "decisions.jsonl"
                        ).verify_integrity(),
                        baseline_count,
                    )
                    self.assertEqual(
                        len(PaperBook.load(fixture.workspace / "paper_book.json").tickets),
                        1,
                    )

    def test_duplicate_execution_marker_is_rejected_before_admission_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            book = PaperBook.load(fixture.workspace / "paper_book.json")
            original = book.tickets[fixture.execution_ticket_id]
            book.open_ticket(
                original.legs,
                original.stake,
                reason=original.strategy_reason,
                placed_at=original.placed_at,
                provider_source_ids=original.provider_source_ids,
                provider_accounts=original.provider_accounts,
                bankroll_id=original.bankroll_id,
                currency=original.currency,
            )
            book.save(fixture.workspace / "paper_book.json")

            with self.assertRaisesRegex(PaperCampaignAdmissionError, "exactly one canonical"):
                fixture.admit(fixture.coordinator())
            self.assertEqual(
                JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verify_integrity(),
                1,
            )

    def test_crash_after_execution_materialization_before_admission_commit_converges_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = AdmissionFixture(Path(directory))
            coordinator = fixture.coordinator()
            with patch.object(
                PaperCampaignRuntime,
                "begin_and_bind_paper_ticket",
                autospec=True,
                side_effect=RuntimeError("crash before campaign action"),
            ):
                with self.assertRaisesRegex(RuntimeError, "crash before campaign action"):
                    fixture.admit(coordinator)
            self.assertEqual(len(PaperBook.load(fixture.workspace / "paper_book.json").tickets), 1)
            self.assertEqual(
                JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verify_integrity(),
                2,
            )

            receipt = fixture.admit(fixture.coordinator(resumed=True))
            self.assertEqual(receipt.ticket_id, fixture.execution_ticket_id)
            self.assertEqual(len(PaperBook.load(fixture.workspace / "paper_book.json").tickets), 1)
            self.assertEqual(
                JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verify_integrity(),
                2,
            )


if __name__ == "__main__":
    unittest.main()
