from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from autosport import _paper_execution_decision_origin as origin_module
from autosport import _paper_execution_decision_origin_callsite_guard as callsite_guard
from autosport.decision_ledger import EconomicDecisionAuthority, JsonlDecisionLedger
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
from autosport.paper_execution_reality import PaperExecutionLedger
from autosport.risk import PaperRiskPolicy

from test_live_decision_loop import (
    PersistentLiveDecisionLoopTests,
    _DurableObserver,
    _ManualClock,
    _PositiveIntentFactory,
)
from test_paper_execution_decision_origin_product_paths import _execution_model


def _live_authority() -> EconomicDecisionAuthority:
    goal = EconomicGoalContract(
        goal_id="goal-live-origin-workspace-binding",
        revision=1,
        bankroll_id="bankroll-live-test",
        currency="EUR",
        max_risk_of_ruin=Decimal("1"),
    )
    return EconomicDecisionAuthority(
        goal,
        PaperRiskPolicy(economic_goal=goal),
    )


def test_workspace_decision_ledger_guard_rejects_post_construction_path_mutation(
    tmp_path,
) -> None:
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    callsite_guard._require_workspace_decision_ledger(
        ledger,
        tmp_path,
        producer="test",
    )

    ledger.path = tmp_path / "alternate-decisions.jsonl"

    with pytest.raises(
        origin_module.PaperExecutionDecisionOriginError,
        match="canonical workspace decisions.jsonl",
    ):
        callsite_guard._require_workspace_decision_ledger(
            ledger,
            tmp_path,
            producer="test",
        )


@pytest.mark.parametrize("replace_ledger", [False, True])
def test_live_product_path_rejects_alternate_decision_ledger_authority(
    tmp_path,
    replace_ledger: bool,
) -> None:
    fixture = PersistentLiveDecisionLoopTests()
    event = fixture._event(selection="selection-a", sequence=1)
    clock = _ManualClock(fixture.START + timedelta(seconds=1))
    book = PaperBook("1000")
    execution_ledger = PaperExecutionLedger(tmp_path / "paper-execution.jsonl")
    execution = PaperExecutionAdoptionRuntime(
        book=book,
        ledger=execution_ledger,
        config=_execution_model(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=tmp_path / "paper_book.json",
    )
    loop = fixture._loop(
        tmp_path,
        observer=_DurableObserver(tmp_path, [(event,)]),
        factory=_PositiveIntentFactory(fixture.INTENT_CONFIG_SHA256),
        clock=clock,
        book=book,
        authority=_live_authority(),
        paper_execution=execution,
    )
    loop.register_input("input-a", selection_ids="selection-a")

    alternate_path = tmp_path / "alternate-decisions.jsonl"
    if replace_ledger:
        loop.decision_ledger = JsonlDecisionLedger(alternate_path)
    else:
        loop.decision_ledger.path = alternate_path

    with pytest.raises(
        origin_module.PaperExecutionDecisionOriginError,
        match="canonical workspace decisions.jsonl",
    ):
        loop.run_cycle()

    assert execution_ledger.events() == ()
