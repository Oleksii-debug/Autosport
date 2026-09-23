from __future__ import annotations

from dataclasses import replace

import pytest

from autosport import _paper_execution_decision_origin as origin_module
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime

from test_paper_execution_decision_origin_product_paths import (
    _paper_value_agent,
    _paper_value_context,
    _paper_value_event,
    _paper_value_goal,
)


def test_paper_value_product_path_rejects_replaced_decision_ledger_after_runtime_binding(
    tmp_path,
) -> None:
    goal = _paper_value_goal()
    first_event = _paper_value_event()
    canonical_ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    context = _paper_value_context(
        tmp_path,
        event=first_event,
        replay_run_id="run-origin-paper-value-workspace-binding",
        ledger=canonical_ledger,
    )

    _paper_value_agent(first_event, goal).on_market_event(first_event, context)

    runtime = context.paper_execution
    assert isinstance(runtime, PaperExecutionAdoptionRuntime)
    before = runtime.ledger.events()
    assert before

    second_event = replace(
        first_event,
        selection_id="selection-origin-paper-value-2",
        sequence=2,
        source_ts="2026-09-17T15:00:00+00:00",
        observed_ts="2026-09-17T15:00:01+00:00",
        ingest_ts="2026-09-17T15:00:01+00:00",
    )
    context.market_mirror.apply(second_event)
    context.decision_ledger = JsonlDecisionLedger(
        tmp_path / "alternate-decisions.jsonl"
    )

    with pytest.raises(
        origin_module.PaperExecutionDecisionOriginError,
        match="canonical workspace decisions.jsonl",
    ):
        _paper_value_agent(second_event, goal).on_market_event(second_event, context)

    assert runtime.ledger.events() == before
