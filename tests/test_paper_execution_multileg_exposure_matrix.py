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


QUOTE_AT = "2026-09-21T17:25:00+00:00"
STARTED_AT = "2026-09-21T17:25:00.100000+00:00"
EXPIRES_AT = "2026-09-21T17:26:00+00:00"


def _action(action_id: str, stake: str) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-multileg",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds="2.50",
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="multileg-exposure-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-multileg-exposure",
        approval_id="paper-only-no-real-money",
        created_at=QUOTE_AT,
        actions=(
            _action("a1", "10.00"),
            _action("a2", "20.00"),
            _action("a3", "40.00"),
        ),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="multileg-exposure-v1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="multileg-exposure-regression",
        seed="multileg-exposure-fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        unknown_bps=1_000,
        rejected_bps=2_000,
        partial_bps=3_000,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


class PaperExecutionMultilegExposureTests(unittest.TestCase):
    def test_terminal_partial_or_unknown_after_accepted_prefix_stops_and_preserves_capital_at_risk(self):
        cases = (
            (
                "partial",
                (9_000, 4_000),
                PaperAttemptOutcome.PARTIAL,
                Decimal("10.00"),
                Decimal("20.00"),
            ),
            (
                "unknown",
                (9_000, 500),
                PaperAttemptOutcome.UNKNOWN,
                None,
                Decimal("30.00"),
            ),
        )

        for (
            case_name,
            buckets,
            terminal_outcome,
            terminal_execution_stake,
            expected_worst_case,
        ) in cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
                with patch(
                    "autosport.paper_execution_reality._impl._deterministic_int",
                    side_effect=buckets,
                ) as deterministic_int:
                    result = execute_paper_plan(
                        plan=_plan(),
                        trigger_id=f"trigger-multileg-{case_name}",
                        config=_config(),
                        ledger=ledger,
                        started_at=STARTED_AT,
                    )

                self.assertTrue(result.completed)
                self.assertEqual(len(result.attempts), 2)
                self.assertEqual(
                    [attempt.outcome for attempt in result.attempts],
                    [PaperAttemptOutcome.ACCEPTED, terminal_outcome],
                )
                self.assertEqual(result.attempts[0].execution_stake, Decimal("10.00"))
                self.assertEqual(
                    result.attempts[1].execution_stake,
                    terminal_execution_stake,
                )
                self.assertEqual(result.pending_action_ids, ("a3",))
                self.assertEqual(result.worst_case_exposure, expected_worst_case)
                self.assertEqual(
                    result.recovery_decision,
                    RecoveryDecision.HEDGE_REVIEW_REQUIRED,
                )
                self.assertEqual(deterministic_int.call_count, 2)
                self.assertEqual(
                    [call.args[1:] for call in deterministic_int.call_args_list],
                    [("outcome", 10_000), ("outcome", 10_000)],
                )


if __name__ == "__main__":
    unittest.main()
