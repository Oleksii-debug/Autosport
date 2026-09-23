from __future__ import annotations

import pytest

from autosport.betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    resolve_betfair_standard_limit_price_bound,
)
from autosport.betfair_standard_limit_price_bound_product_verifier import (
    verify_product_betfair_standard_limit_price_bound,
)
from autosport.economic_goal_store import EconomicGoalStore
from autosport.real_execution_ledger import RealExecutionLedger
from autosport.supervised_plan_issuance import (
    SupervisedPlanIssuanceError,
    SupervisedPlanIssuanceStore,
)

from test_betfair_supervised_execution import _bound, _profile


def test_caller_created_workspace_cannot_become_product_issuance_root(
    tmp_path,
) -> None:
    """Durable self-consistency must not manufacture product decision/approval origin."""

    # Every object below is reachable through public product APIs.  The workspace
    # contains a real EconomicGoalStore contract; no monkeypatch or alternate
    # implementation is used to manufacture a positive path.
    bound, approval, goal = _bound(_profile())
    workspace = tmp_path / "caller-workspace"
    workspace.mkdir()
    EconomicGoalStore(workspace).initialize_owner(goal)

    issuance_store = SupervisedPlanIssuanceStore(
        workspace,
        authority_root=tmp_path / "caller-authority-root",
    )
    ledger = RealExecutionLedger(tmp_path / "caller-execution-ledger.jsonl")
    action = bound.execution_plan.actions[0]

    # Desired authority boundary: this caller-created durable universe must fail
    # closed somewhere before positive product verification unless the exact
    # decision and approval were first emitted by canonical product producers.
    # Parent #750 currently accepts the full chain, so this is intentionally an
    # expected-red falsifier until that existing trust-root blocker is repaired.
    with pytest.raises(
        (SupervisedPlanIssuanceError, BetfairStandardLimitPriceBoundError)
    ):
        issuance_store.issue(bound=bound, approval=approval)
        ledger.reserve_plan(bound.execution_plan)
        ledger.bind_supervised_approval(
            plan_id=bound.execution_plan.plan_id,
            approval_id=approval.ledger_identity,
            approval_fingerprint=approval.fingerprint,
            approved_at=approval.approved_at,
            evidence_sha256=approval.evidence_sha256,
        )
        evidence = resolve_betfair_standard_limit_price_bound(
            bound=bound,
            action_id=action.action_id,
        )
        verify_product_betfair_standard_limit_price_bound(
            evidence=evidence,
            ledger=ledger,
            issuance_store=issuance_store,
            execution_plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )
