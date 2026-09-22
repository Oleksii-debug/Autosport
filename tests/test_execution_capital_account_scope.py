from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.execution_capital_at_risk import (
    ExecutionCapitalAtRiskUnsupported,
    resolve_execution_capital_at_risk,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


CREATED_AT = "2026-09-22T08:30:00+00:00"
RESERVED_AT = "2026-09-22T08:30:01+00:00"
SUBMITTED_AT = "2026-09-22T08:30:02+00:00"
EXPIRES_AT = "2026-09-22T09:30:00+00:00"


def _action(*, action_id: str, account_id: str, stake: str) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="betfair",
        account_id=account_id,
        event_id="event-1",
        market_id="1.234",
        selection_id=action_id,
        side="BACK",
        requested_odds="2",
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=CREATED_AT,
        expires_at=EXPIRES_AT,
    )


def test_cross_account_attempts_cannot_be_summed_without_common_denomination(
    tmp_path,
) -> None:
    action_a = _action(action_id="selection-a", account_id="account-a", stake="10")
    action_b = _action(action_id="selection-b", account_id="account-b", stake="20")
    plan = ExecutionPlan(
        plan_id="plan-cross-account-risk",
        bookmaker_profile_version="betfair-profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=CREATED_AT,
        actions=(action_a, action_b),
    )
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    ledger.reserve_plan(plan)

    for index, action in enumerate(plan.actions, start=1):
        attempt_id = f"attempt-{index}"
        ledger.begin_attempt(
            plan_id=plan.plan_id,
            action_id=action.action_id,
            attempt_id=attempt_id,
            reserved_at=RESERVED_AT,
        )
        ledger.mark_submitted(attempt_id, submitted_at=SUBMITTED_AT)

    with pytest.raises(
        ExecutionCapitalAtRiskUnsupported,
        match="account|denomination|scope",
    ):
        resolve_execution_capital_at_risk(ledger, plan.plan_id)

    # Without a common-denomination authority, Decimal("10") from account-a
    # and Decimal("20") from account-b are not mechanically one Decimal("30")
    # capital quantity, even though both provider actions are Betfair BACK.
    assert action_a.requested_stake == Decimal("10")
    assert action_b.requested_stake == Decimal("20")
