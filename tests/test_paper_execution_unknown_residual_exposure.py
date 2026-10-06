from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    RecoveryDecision,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T03:00:00+00:00"
STARTED_AT = "2026-09-20T03:00:00.100000+00:00"
OBSERVED_AT = "2026-09-20T03:00:00.250000+00:00"
EXPIRES_AT = "2026-09-20T03:01:00+00:00"


def _action(action_id: str, *, stake: str) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-1",
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
        plan_id="paper-unknown-residual-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-unknown-residual",
        approval_id="paper-only",
        created_at=QUOTE_AT,
        actions=(
            _action("a1", stake="7.25"),
            _action("a2", stake="11.75"),
            _action("a3", stake="5.00"),
        ),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="2",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="test-seeded-model",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _record(
    action: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    accepted_stake: str | None = None,
) -> PaperExecutionEvidenceRecord:
    return PaperExecutionEvidenceRecord(
        action_id=action.action_id,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        quote_id=action.quote_id,
        outcome=outcome,
        observed_at=OBSERVED_AT,
        evidence_grade=EvidenceGrade.EMPIRICAL,
        evidence_source=f"captured-{action.action_id}-{outcome.value.lower()}",
        accepted_odds="2.40" if accepted_stake is not None else None,
        accepted_stake=accepted_stake,
        reason=f"observed {outcome.value.lower()}",
    )


class PaperExecutionUnknownResidualExposureTests(unittest.TestCase):
    def test_accepted_then_unknown_counts_full_ambiguous_leg_and_stops_before_next_leg(self):
        current = _plan()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            path = workspace / "paper-execution.jsonl"
            with patch.dict(
                os.environ,
                {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(root / "authority")},
                clear=False,
            ):
                ledger = PaperExecutionLedger(path)
                registry = PaperExecutionEvidenceRegistry(ledger)
                accepted = _record(
                    current.actions[0],
                    PaperAttemptOutcome.ACCEPTED,
                    accepted_stake="7.25",
                )
                unknown = _record(
                    current.actions[1],
                    PaperAttemptOutcome.UNKNOWN,
                )
                registry.register(accepted)
                registry.register(unknown)

                first = execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-accepted-then-unknown",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={
                        "a1": accepted.as_observation(),
                        "a2": unknown.as_observation(),
                    },
                    evidence_registry=registry,
                )

                self.assertEqual(
                    [attempt.outcome for attempt in first.attempts],
                    [PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.UNKNOWN],
                )
                self.assertEqual(first.pending_action_ids, ("a3",))
                self.assertEqual(first.worst_case_exposure, Decimal("19.00"))
                self.assertEqual(
                    first.recovery_decision,
                    RecoveryDecision.HEDGE_REVIEW_REQUIRED,
                )
                self.assertTrue(first.completed)
                self.assertNotIn(
                    "a3",
                    [attempt.action_id for attempt in first.attempts],
                )

                event_count = len(ledger.events())
                restarted_ledger = PaperExecutionLedger(path)
                restarted_registry = PaperExecutionEvidenceRegistry(restarted_ledger)
                restarted = execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-accepted-then-unknown",
                    config=_config(),
                    ledger=restarted_ledger,
                    started_at=STARTED_AT,
                    observations={
                        "a1": accepted.as_observation(),
                        "a2": unknown.as_observation(),
                    },
                    evidence_registry=restarted_registry,
                )

                self.assertEqual(restarted, first)
                self.assertEqual(restarted.worst_case_exposure, Decimal("19.00"))
                self.assertEqual(len(restarted_ledger.events()), event_count)


if __name__ == "__main__":
    unittest.main()
