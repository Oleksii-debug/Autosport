import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)
from autosport.startup_execution_gate import (
    AccountExposureSnapshot,
    ExecutionAccount,
    StartupExecutionGate,
)


OBSERVED_AT = "2026-09-20T10:00:00+00:00"
RESERVED_AT = "2026-09-20T10:00:01+00:00"
EXPIRES_AT = "2099-01-01T00:00:00+00:00"
ACCOUNT_OBSERVED_AT = "2026-09-21T14:37:00+00:00"
ACCOUNT = ExecutionAccount("betfair", "acct-1")


def _plan() -> ExecutionPlan:
    action = ExecutionAction(
        action_id="race-action",
        bookmaker_id=ACCOUNT.bookmaker_id,
        account_id=ACCOUNT.account_id,
        event_id="race-event",
        market_id="race-market",
        selection_id="race-selection",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="race-quote",
        quote_observed_at=OBSERVED_AT,
        expires_at=EXPIRES_AT,
    )
    return ExecutionPlan(
        plan_id="race-plan",
        bookmaker_profile_version="profile-v1",
        decision_id="race-decision",
        approval_id="race-approval",
        created_at=OBSERVED_AT,
        actions=(action,),
    )


def _account_snapshot() -> AccountExposureSnapshot:
    return AccountExposureSnapshot(
        bookmaker_id=ACCOUNT.bookmaker_id,
        account_id=ACCOUNT.account_id,
        currency="EUR",
        available_bankroll="100.00",
        open_exposure="0.00",
        observed_at=ACCOUNT_OBSERVED_AT,
    )


class StartupExecutionGateFinalSnapshotRaceTests(unittest.TestCase):
    def test_attempt_appended_after_final_snapshot_cannot_escape_startup_gate(self):
        """A post-snapshot durable attempt must prevent positive startup admission.

        The hook models another execution writer committing immediately after the
        gate captures its final verified ledger bytes. The current implementation
        scans only attempt IDs present in that captured payload, so the later
        durable RESERVED attempt is invisible even though it exists before
        evaluate() returns execution_enabled=True.
        """

        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            original_verified_snapshot = ledger.verified_snapshot
            snapshot_calls = 0

            def verified_snapshot_with_post_capture_append():
                nonlocal snapshot_calls
                snapshot = original_verified_snapshot()
                snapshot_calls += 1
                if snapshot_calls == 2:
                    ledger.reserve_plan(_plan())
                    ledger.begin_attempt(
                        plan_id="race-plan",
                        action_id="race-action",
                        attempt_id="race-attempt",
                        reserved_at=RESERVED_AT,
                    )
                return snapshot

            gate = StartupExecutionGate(
                ledger=ledger,
                expected_accounts=(ACCOUNT,),
                reconcile_unresolved=lambda _attempt_ids: None,
                rebuild_account_state=lambda: (_account_snapshot(),),
            )

            with patch.object(
                ledger,
                "verified_snapshot",
                side_effect=verified_snapshot_with_post_capture_append,
            ):
                status = gate.evaluate()

            self.assertEqual(snapshot_calls, 2)
            self.assertEqual(
                ledger.attempt_state("race-attempt"),
                AttemptState.RESERVED,
            )
            self.assertFalse(
                status.execution_enabled,
                "startup gate enabled execution from a stale final ledger snapshot",
            )
            self.assertEqual(status.unresolved_attempt_ids, ("race-attempt",))


if __name__ == "__main__":
    unittest.main()
