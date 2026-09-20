from decimal import Decimal

import pytest

from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


def test_execute_rejects_caller_constructed_prepared_execution_before_runtime_use() -> None:
    action = ExecutionAction(
        action_id="forged-action",
        bookmaker_id="provider-a",
        account_id="account-a",
        event_id="event-a",
        market_id="market-a",
        selection_id="selection-a",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("9999"),
        quote_id="quote-a",
        quote_observed_at="2026-09-20T07:00:00+00:00",
        expires_at="2026-09-20T07:01:00+00:00",
    )
    prepared = PreparedPaperExecution(
        execution_plan=ExecutionPlan(
            plan_id="forged-plan",
            bookmaker_profile_version="paper-execution-reality:test:v1",
            decision_id="forged-decision",
            approval_id="paper-only-no-real-money",
            created_at="2026-09-20T07:00:00+00:00",
            actions=(action,),
        ),
        exposure_bindings=(
            PaperExposureBinding(
                action_id=action.action_id,
                sport="football",
                bankroll_id="bankroll-a",
                currency="EUR",
            ),
        ),
        intent_evidence_json='{"forged":true}',
    )

    # Bypass __init__ deliberately: a correct authority check must reject the
    # caller-constructed prepared value before touching config, ledger, or book.
    runtime = object.__new__(PaperExecutionAdoptionRuntime)
    runtime._prepared_authorities = {}

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="not minted by this runtime from canonical authority",
    ):
        runtime.execute(
            prepared=prepared,
            trigger_id="forged-trigger",
            started_at="2026-09-20T07:00:01+00:00",
            materialize_exposure=True,
        )
