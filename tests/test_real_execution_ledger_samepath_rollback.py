from __future__ import annotations

import pytest

from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    RealExecutionLedger,
)


TS = "2026-09-17T19:28:00+00:00"
RESERVED_AT = "2026-09-17T19:28:10+00:00"
EXPIRES_AT = "2026-09-17T19:29:00+00:00"


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-rollback",
        bookmaker_id="betfair",
        account_id="acct-rollback",
        event_id="event-rollback",
        market_id="market-rollback",
        selection_id="selection-rollback",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="quote-rollback",
        quote_observed_at=TS,
        expires_at=EXPIRES_AT,
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-rollback",
        bookmaker_profile_version="profile-v1",
        decision_id="decision-rollback",
        approval_id="approval-rollback",
        created_at=TS,
        actions=(_action(),),
    )


def test_same_path_valid_prefix_rollback_cannot_become_current_after_restart(
    tmp_path,
) -> None:
    path = tmp_path / "execution.jsonl"
    ledger = RealExecutionLedger(path)
    current_plan = _plan()

    ledger.reserve_plan(current_plan)
    snapshot_s1 = ledger.verified_snapshot()
    bytes_s1 = path.read_bytes()

    ledger.begin_attempt(
        plan_id=current_plan.plan_id,
        action_id=current_plan.actions[0].action_id,
        attempt_id="attempt-rollback",
        reserved_at=RESERVED_AT,
    )
    snapshot_s2 = ledger.verified_snapshot()

    assert snapshot_s2.sha256 != snapshot_s1.sha256
    assert snapshot_s2.event_count > snapshot_s1.event_count

    # Simulate a complete-valid workspace rollback: the protected ledger path is
    # restored to a byte-exact older prefix while the independent machine-state
    # high-water authority must still remember that S2 was durably published.
    path.write_bytes(bytes_s1)
    reopened = RealExecutionLedger(path)

    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="rollback|monotonic|older|high-water|committed",
    ):
        reopened.verified_snapshot()
