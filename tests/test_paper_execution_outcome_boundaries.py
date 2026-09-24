from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    RecoveryDecision,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-21T17:20:00+00:00"
STARTED_AT = "2026-09-21T17:20:00.100000+00:00"
EXPIRES_AT = "2026-09-21T17:21:00+00:00"


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="outcome-boundary-action",
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-boundary",
        market_id="market-boundary",
        selection_id="selection-boundary",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="quote-boundary",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="outcome-boundary-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-outcome-boundary",
        approval_id="paper-only-no-real-money",
        created_at=QUOTE_AT,
        actions=(_action(),),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="outcome-boundary-v1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="outcome-boundary-regression",
        seed="outcome-boundary-fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        unknown_bps=1_000,
        rejected_bps=2_000,
        partial_bps=3_000,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


class PaperExecutionOutcomeBoundaryTests(unittest.TestCase):
    def test_exact_bucket_boundaries_preserve_outcome_and_exposure_semantics(self):
        cases = (
            (
                999,
                PaperAttemptOutcome.UNKNOWN,
                None,
                None,
                Decimal("10.00"),
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            ),
            (
                1_000,
                PaperAttemptOutcome.REJECTED,
                None,
                None,
                Decimal("0"),
                RecoveryDecision.NO_EXPOSURE,
            ),
            (
                2_999,
                PaperAttemptOutcome.REJECTED,
                None,
                None,
                Decimal("0"),
                RecoveryDecision.NO_EXPOSURE,
            ),
            (
                3_000,
                PaperAttemptOutcome.PARTIAL,
                Decimal("2.50"),
                Decimal("5.00"),
                Decimal("5.00"),
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            ),
            (
                5_999,
                PaperAttemptOutcome.PARTIAL,
                Decimal("2.50"),
                Decimal("5.00"),
                Decimal("5.00"),
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            ),
            (
                6_000,
                PaperAttemptOutcome.ACCEPTED,
                Decimal("2.50"),
                Decimal("10.00"),
                Decimal("10.00"),
                RecoveryDecision.NONE,
            ),
            (
                9_999,
                PaperAttemptOutcome.ACCEPTED,
                Decimal("2.50"),
                Decimal("10.00"),
                Decimal("10.00"),
                RecoveryDecision.NONE,
            ),
        )

        for (
            bucket,
            expected_outcome,
            expected_odds,
            expected_stake,
            expected_worst_case,
            expected_recovery,
        ) in cases:
            with self.subTest(bucket=bucket), tempfile.TemporaryDirectory() as tmp:
                ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
                with patch(
                    "autosport.paper_execution_reality._impl._deterministic_int",
                    return_value=bucket,
                ) as deterministic_int:
                    result = execute_paper_plan(
                        plan=_plan(),
                        trigger_id=f"trigger-outcome-boundary-{bucket}",
                        config=_config(),
                        ledger=ledger,
                        started_at=STARTED_AT,
                    )

                self.assertTrue(result.completed)
                self.assertEqual(result.pending_action_ids, ())
                self.assertEqual(len(result.attempts), 1)
                attempt = result.attempts[0]
                self.assertEqual(attempt.outcome, expected_outcome)
                self.assertEqual(attempt.execution_odds, expected_odds)
                self.assertEqual(attempt.execution_stake, expected_stake)
                self.assertEqual(result.worst_case_exposure, expected_worst_case)
                self.assertEqual(result.recovery_decision, expected_recovery)
                deterministic_int.assert_called_once()
                self.assertEqual(deterministic_int.call_args.args[1:], ("outcome", 10_000))


if __name__ == "__main__":
    unittest.main()
