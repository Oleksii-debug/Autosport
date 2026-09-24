from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-21T17:00:00+00:00"
STARTED_AT = "2026-09-21T17:00:00.100000+00:00"
EXPIRES_AT = "2026-09-21T17:01:00+00:00"


def _runtime(root: Path, book: PaperBook) -> PaperExecutionAdoptionRuntime:
    return PaperExecutionAdoptionRuntime(
        book=book,
        ledger=PaperExecutionLedger(root / "paper-execution.jsonl"),
        config=PaperExecutionModelConfig(
            model_id="paper-reality",
            model_version="corrupt-pre-action-witness-v1",
            evidence_grade=EvidenceGrade.SYNTHETIC,
            evidence_source="corrupt-pre-action-witness-test",
            seed="corrupt-pre-action-witness-fixed-seed",
            max_quote_age_ms=5_000,
            min_delay_ms=100,
            max_delay_ms=100,
            rejected_bps=0,
            partial_bps=0,
            unknown_bps=0,
            partial_fill_bps=5_000,
            max_slippage_bps=0,
        ),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=root / "paper_book.json",
    )


def _prepared(
    runtime: PaperExecutionAdoptionRuntime,
) -> PreparedPaperExecution:
    action = ExecutionAction(
        action_id="corrupt-witness-action",
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-main",
        market_id="market-main",
        selection_id="selection-main",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="quote-main",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )
    return runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=ExecutionPlan(
                plan_id="corrupt-witness-plan",
                bookmaker_profile_version="paper-profile-v1",
                decision_id="decision-corrupt-witness",
                approval_id="paper-only-no-real-money",
                created_at=QUOTE_AT,
                actions=(action,),
            ),
            exposure_bindings=(
                PaperExposureBinding(
                    action_id=action.action_id,
                    sport="soccer",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                ),
            ),
            intent_evidence_json='{"schema":"corrupt-pre-action-witness-test"}',
        )
    )


class PaperExecutionPreActionWitnessCorruptionTests(unittest.TestCase):
    def test_live_retry_rejects_corrupt_pre_action_witness_before_book_mutation(self):
        corrupt_payloads = (
            ("invalid_utf8", b"\xff"),
            ("invalid_json", b"{"),
            ("invalid_snapshot_schema", b"{}"),
        )

        for materialize_exposure in (False, True):
            for case_name, payload in corrupt_payloads:
                with (
                    self.subTest(
                        materialize_exposure=materialize_exposure,
                        case=case_name,
                    ),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    root = Path(tmp)
                    book = PaperBook("100.00")
                    runtime = _runtime(root, book)
                    witness_path = root / "live_decision_pre_action_book.json"
                    witness_path.write_bytes(payload)

                    with self.assertRaisesRegex(
                        PaperExecutionAdoptionError,
                        "live recovery pre-action PaperBook is unreadable",
                    ):
                        runtime.execute(
                            prepared=_prepared(runtime),
                            trigger_id=(
                                "live-corrupt-pre-action-witness-"
                                f"{materialize_exposure}-{case_name}"
                            ),
                            started_at=STARTED_AT,
                            materialize_exposure=materialize_exposure,
                        )

                    self.assertEqual(book.balance, Decimal("100.00"))
                    self.assertEqual(book.tickets, {})


if __name__ == "__main__":
    unittest.main()
