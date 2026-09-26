from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionPlan,
    ExecutionStateError,
    RealExecutionLedger,
)


RESERVED_AT = "2026-09-21T18:30:00+00:00"
SECOND_RESERVED_AT = "2026-09-21T18:31:00+00:00"
QUOTE_AT = "2026-09-21T18:29:00+00:00"
EXPIRES_AT = "2026-09-21T18:40:00+00:00"


def _action(action_id: str) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="book-1",
        account_id="account-1",
        event_id=f"event-{action_id}",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds="2.00",
        requested_stake="10.00",
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _plan(plan_id: str, action_id: str) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        bookmaker_profile_version="profile-v1",
        decision_id=f"decision-{plan_id}",
        approval_id=f"approval-{plan_id}",
        created_at=QUOTE_AT,
        actions=(_action(action_id),),
    )


def _ledger_with_two_plans(path: Path) -> RealExecutionLedger:
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(_plan("plan-1", "action-1"))
    ledger.reserve_plan(_plan("plan-2", "action-2"))
    return ledger


class CrossInstanceSnapshotCasTests(unittest.TestCase):
    def test_stale_snapshot_cannot_create_attempt_from_second_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            first_writer = _ledger_with_two_plans(path)
            second_writer = RealExecutionLedger(path)
            admitted = first_writer.verified_snapshot()

            first_writer.begin_attempt(
                plan_id="plan-1",
                action_id="action-1",
                attempt_id="attempt-1",
                reserved_at=RESERVED_AT,
                expected_snapshot_sha256=admitted.sha256,
            )
            after_first = first_writer.verified_snapshot()

            with self.assertRaisesRegex(
                ExecutionStateError,
                "snapshot changed; recompute admission",
            ):
                second_writer.begin_attempt(
                    plan_id="plan-2",
                    action_id="action-2",
                    attempt_id="attempt-2",
                    reserved_at=SECOND_RESERVED_AT,
                    expected_snapshot_sha256=admitted.sha256,
                )

            self.assertEqual(second_writer.verified_snapshot(), after_first)
            with self.assertRaises(KeyError):
                second_writer.attempt_state("attempt-2")

            fresh = second_writer.verified_snapshot()
            second_writer.begin_attempt(
                plan_id="plan-2",
                action_id="action-2",
                attempt_id="attempt-2",
                reserved_at=SECOND_RESERVED_AT,
                expected_snapshot_sha256=fresh.sha256,
            )
            self.assertEqual(
                second_writer.attempt_state("attempt-2"),
                AttemptState.RESERVED,
            )

    def test_cross_instance_exact_replay_remains_idempotent_after_ledger_advance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            first_writer = _ledger_with_two_plans(path)
            second_writer = RealExecutionLedger(path)
            replay_writer = RealExecutionLedger(path)
            original_snapshot = first_writer.verified_snapshot()

            first = first_writer.begin_attempt(
                plan_id="plan-1",
                action_id="action-1",
                attempt_id="attempt-1",
                reserved_at=RESERVED_AT,
                expected_snapshot_sha256=original_snapshot.sha256,
            )

            fresh = second_writer.verified_snapshot()
            second_writer.begin_attempt(
                plan_id="plan-2",
                action_id="action-2",
                attempt_id="attempt-2",
                reserved_at=SECOND_RESERVED_AT,
                expected_snapshot_sha256=fresh.sha256,
            )
            before_replay = replay_writer.verified_snapshot()

            replay = replay_writer.begin_attempt(
                plan_id="plan-1",
                action_id="action-1",
                attempt_id="attempt-1",
                reserved_at=SECOND_RESERVED_AT,
                expected_snapshot_sha256=original_snapshot.sha256,
            )

            self.assertEqual(replay, first)
            self.assertEqual(replay_writer.verified_snapshot(), before_replay)
            self.assertEqual(
                replay_writer.attempt_state("attempt-1"),
                AttemptState.RESERVED,
            )
            self.assertEqual(
                replay_writer.attempt_state("attempt-2"),
                AttemptState.RESERVED,
            )


if __name__ == "__main__":
    unittest.main()
