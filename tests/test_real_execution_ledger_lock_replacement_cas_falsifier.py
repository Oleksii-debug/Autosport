from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionLedgerError,
    ExecutionPlan,
    RealExecutionLedger,
)


RESERVED_AT = "2026-09-22T03:15:00+00:00"
QUOTE_AT = "2026-09-22T03:14:00+00:00"
EXPIRES_AT = "2026-09-22T03:25:00+00:00"


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


@unittest.skipIf(os.name == "nt", "requires POSIX unlink semantics")
class WriterLockReplacementCasFalsifierTests(unittest.TestCase):
    def test_replaced_writer_lock_cannot_admit_two_attempts_from_one_snapshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            bootstrap = RealExecutionLedger(path)
            bootstrap.reserve_plan(_plan("plan-1", "action-1"))
            bootstrap.reserve_plan(_plan("plan-2", "action-2"))
            admitted = bootstrap.verified_snapshot()

            first = RealExecutionLedger(path)
            second = RealExecutionLedger(path)

            entered_append = threading.Event()
            release_first = threading.Event()
            first_errors: list[BaseException] = []
            original_append = first._append

            def blocking_append(
                kind,
                plan_id,
                action_id,
                attempt_id,
                payload,
            ) -> None:
                if kind.value == "ATTEMPT_RESERVED":
                    entered_append.set()
                    if not release_first.wait(10):
                        raise AssertionError("timed out waiting to resume first writer")
                original_append(kind, plan_id, action_id, attempt_id, payload)

            first._append = blocking_append  # type: ignore[method-assign]

            def run_first() -> None:
                try:
                    first.begin_attempt(
                        plan_id="plan-1",
                        action_id="action-1",
                        attempt_id="attempt-1",
                        reserved_at=RESERVED_AT,
                        expected_snapshot_sha256=admitted.sha256,
                    )
                except BaseException as exc:  # pragma: no cover - thread handoff
                    first_errors.append(exc)

            worker = threading.Thread(target=run_first, daemon=True)
            worker.start()
            self.assertTrue(
                entered_append.wait(10),
                "first writer never reached the post-CAS append boundary",
            )
            self.assertTrue(first._lock_path.exists())

            second_result = None
            second_error: BaseException | None = None
            try:
                first._lock_path.unlink()
                try:
                    second_result = second.begin_attempt(
                        plan_id="plan-2",
                        action_id="action-2",
                        attempt_id="attempt-2",
                        reserved_at=RESERVED_AT,
                        expected_snapshot_sha256=admitted.sha256,
                    )
                except ExecutionLedgerError as exc:
                    second_error = exc
            finally:
                release_first.set()
                worker.join(10)

            self.assertFalse(worker.is_alive(), "first writer failed to finish")
            self.assertFalse(first_errors, first_errors)

            if second_result is not None:
                self.assertEqual(
                    first.attempt_state("attempt-1"),
                    AttemptState.RESERVED,
                )
                self.assertEqual(
                    second.attempt_state("attempt-2"),
                    AttemptState.RESERVED,
                )

            self.assertIsNone(
                second_result,
                "replacing the writer-lock pathname admitted a second "
                "ATTEMPT_RESERVED against the same exact ledger snapshot",
            )
            self.assertIsNotNone(
                second_error,
                "lock replacement must fail closed before a second reservation",
            )


if __name__ == "__main__":
    unittest.main()
