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


class SnapshotBoundAttemptReservationTests(unittest.TestCase):
    def _ledger_with_two_plans(self, path: Path) -> RealExecutionLedger:
        ledger = RealExecutionLedger(path)
        ledger.reserve_plan(_plan("plan-1", "action-1"))
        ledger.reserve_plan(_plan("plan-2", "action-2"))
        return ledger

    def test_stale_snapshot_cannot_create_second_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_two_plans(Path(tmp) / "execution.jsonl")
            base = ledger.verified_snapshot()

            ledger.begin_attempt(
                plan_id="plan-1",
                action_id="action-1",
                attempt_id="attempt-1",
                reserved_at=RESERVED_AT,
                expected_snapshot_sha256=base.sha256,
            )
            after_first = ledger.verified_snapshot()

            with self.assertRaisesRegex(
                ExecutionStateError,
                "snapshot changed; recompute admission",
            ):
                ledger.begin_attempt(
                    plan_id="plan-2",
                    action_id="action-2",
                    attempt_id="attempt-2",
                    reserved_at=RESERVED_AT,
                    expected_snapshot_sha256=base.sha256,
                )

            self.assertEqual(
                ledger.verified_snapshot().event_count,
                after_first.event_count,
            )
            self.assertEqual(
                ledger.attempt_state("attempt-1"),
                AttemptState.RESERVED,
            )

            fresh = ledger.verified_snapshot()
            ledger.begin_attempt(
                plan_id="plan-2",
                action_id="action-2",
                attempt_id="attempt-2",
                reserved_at=RESERVED_AT,
                expected_snapshot_sha256=fresh.sha256,
            )
            self.assertEqual(
                ledger.attempt_state("attempt-2"),
                AttemptState.RESERVED,
            )

    def test_exact_replay_is_idempotent_even_after_snapshot_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_two_plans(Path(tmp) / "execution.jsonl")
            base = ledger.verified_snapshot()

            first = ledger.begin_attempt(
                plan_id="plan-1",
                action_id="action-1",
                attempt_id="attempt-1",
                reserved_at=RESERVED_AT,
                expected_snapshot_sha256=base.sha256,
            )
            event_count = ledger.verified_snapshot().event_count

            replay = ledger.begin_attempt(
                plan_id="plan-1",
                action_id="action-1",
                attempt_id="attempt-1",
                reserved_at="2026-09-21T18:31:00+00:00",
                expected_snapshot_sha256=base.sha256,
            )

            self.assertEqual(replay, first)
            self.assertEqual(
                ledger.verified_snapshot().event_count,
                event_count,
            )

    def test_restart_rejects_stale_snapshot_before_new_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            ledger = self._ledger_with_two_plans(path)
            base = ledger.verified_snapshot()
            ledger.begin_attempt(
                plan_id="plan-1",
                action_id="action-1",
                attempt_id="attempt-1",
                reserved_at=RESERVED_AT,
                expected_snapshot_sha256=base.sha256,
            )

            restarted = RealExecutionLedger(path)
            before = restarted.verified_snapshot()
            with self.assertRaisesRegex(
                ExecutionStateError,
                "snapshot changed; recompute admission",
            ):
                restarted.begin_attempt(
                    plan_id="plan-2",
                    action_id="action-2",
                    attempt_id="attempt-2",
                    reserved_at=RESERVED_AT,
                    expected_snapshot_sha256=base.sha256,
                )
            self.assertEqual(
                restarted.verified_snapshot().sha256,
                before.sha256,
            )
            self.assertEqual(
                restarted.verified_snapshot().event_count,
                before.event_count,
            )

    def test_invalid_snapshot_digest_is_rejected_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = self._ledger_with_two_plans(Path(tmp) / "execution.jsonl")
            before = ledger.verified_snapshot()
            with self.assertRaisesRegex(
                ValueError,
                "expected_snapshot_sha256",
            ):
                ledger.begin_attempt(
                    plan_id="plan-1",
                    action_id="action-1",
                    attempt_id="attempt-1",
                    reserved_at=RESERVED_AT,
                    expected_snapshot_sha256="not-a-sha256",
                )
            self.assertEqual(ledger.verified_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
