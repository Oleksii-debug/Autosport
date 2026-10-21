from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import autosport.betfair_realized_match as realized_match
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


ATTEMPT_ID = "attempt-plan-type-1"


def _action(*, requested_odds: str = "3.0") -> ExecutionAction:
    return ExecutionAction(
        action_id="action-plan-type-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="10",
        side="BACK",
        requested_odds=requested_odds,
        requested_stake="10",
        quote_id="quote-1",
        quote_observed_at="2026-09-26T19:40:00+00:00",
        expires_at="2026-09-26T20:10:00+00:00",
    )


def _prepared(root: Path) -> tuple[ExecutionPlan, RealExecutionLedger]:
    action = _action()
    plan = ExecutionPlan(
        plan_id="plan-plan-type-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-26T19:39:00+00:00",
        actions=(action,),
    )
    ledger = RealExecutionLedger(root / "real-execution.jsonl")
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id=ATTEMPT_ID,
        reserved_at="2026-09-26T19:41:00+00:00",
    )
    ledger.bind_provider_order_reference(
        attempt_id=ATTEMPT_ID,
        provider_id="betfair",
    )
    ledger.mark_submitted(
        ATTEMPT_ID,
        submitted_at="2026-09-26T19:42:00+00:00",
    )
    return plan, ledger


def test_attempt_binding_rejects_stateful_execution_plan_subclass_before_attribute_read(
    tmp_path: Path,
) -> None:
    plan, ledger = _prepared(tmp_path)
    canonical_actions = plan.actions
    forged_actions = (
        replace(canonical_actions[0], requested_odds="9.9"),
    )

    class StatefulExecutionPlan(ExecutionPlan):
        action_reads = 0

        def __getattribute__(self, name: str):
            if name == "actions":
                cls = type(self)
                cls.action_reads += 1
                if cls.action_reads == 1:
                    return canonical_actions
                return forged_actions
            return super().__getattribute__(name)

    malicious = StatefulExecutionPlan(
        plan_id=plan.plan_id,
        bookmaker_profile_version=plan.bookmaker_profile_version,
        decision_id=plan.decision_id,
        approval_id=plan.approval_id,
        created_at=plan.created_at,
        actions=canonical_actions,
    )
    StatefulExecutionPlan.action_reads = 0

    with pytest.raises(TypeError, match="plan must be exact ExecutionPlan"):
        realized_match._attempt_binding(
            malicious,
            ledger,
            ATTEMPT_ID,
        )

    assert StatefulExecutionPlan.action_reads == 0
