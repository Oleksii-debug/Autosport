from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal

import pytest

from autosport import _paper_execution_decision_origin as origin_module
from autosport import _paper_execution_decision_origin_instance_guard as instance_guard
from autosport.agents import AgentContext
from autosport.decision_ledger import EconomicDecisionAuthority, JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
from autosport.live_decision_loop import LiveCycleStatus, PersistentLiveDecisionLoop
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
)
from autosport.paper_strategy import Forecast, PaperValueAgent
from autosport.risk import PaperRiskPolicy

from test_live_decision_loop import (
    PersistentLiveDecisionLoopTests,
    _DurableObserver,
    _ManualClock,
    _PositiveIntentFactory,
)


def _execution_model() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="decision-origin-product-path",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="decision-origin-product-path-test",
        seed="decision-origin-product-path",
        max_quote_age_ms=5_000,
        min_delay_ms=0,
        max_delay_ms=0,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def test_live_product_callsite_rejects_nested_hook_and_binds_exact_origin(
    tmp_path,
    monkeypatch,
) -> None:
    fixture = PersistentLiveDecisionLoopTests()
    event = fixture._event(selection="selection-a", sequence=1)
    clock = _ManualClock(fixture.START + timedelta(seconds=1))
    model = _execution_model()
    book = PaperBook("1000")
    execution_ledger = PaperExecutionLedger(tmp_path / "paper-execution.jsonl")
    execution = PaperExecutionAdoptionRuntime(
        book=book,
        ledger=execution_ledger,
        config=model,
        max_quote_age=timedelta(seconds=5),
        paper_book_path=tmp_path / "paper_book.json",
    )
    goal = EconomicGoalContract(
        goal_id="goal-live-origin-product-path",
        revision=1,
        bankroll_id="bankroll-live-test",
        currency="EUR",
        max_risk_of_ruin=Decimal("1"),
    )
    authority = EconomicDecisionAuthority(
        goal,
        PaperRiskPolicy(economic_goal=goal),
    )
    nested_rejections: list[str] = []
    captured_execution_plan = []
    forged_origin_calls: list[str] = []

    def nested_post_append_hook() -> None:
        current = inspect.currentframe()
        producer = None
        try:
            producer = current.f_back if current is not None else None
            assert producer is not None
            assert producer.f_code is PersistentLiveDecisionLoop._persist_plan.__code__
            prepared = producer.f_locals["prepared_execution"]
            decision_id = producer.f_locals["decision_id"]
            plan = producer.f_locals["plan"]
            assert prepared is not None
            captured_execution_plan.append(prepared.execution_plan)
            with pytest.raises(
                PaperExecutionAdoptionError,
                match="nested PAPER execution cannot inherit",
            ):
                execution.execute(
                    prepared=prepared,
                    trigger_id=decision_id,
                    started_at=plan.decision_ts,
                    materialize_exposure=True,
                )
            nested_rejections.append(decision_id)
            assert execution_ledger.events() == ()
        finally:
            del current
            del producer

    loop = fixture._loop(
        tmp_path,
        observer=_DurableObserver(tmp_path, [(event,)]),
        factory=_PositiveIntentFactory(fixture.INTENT_CONFIG_SHA256),
        clock=clock,
        post_append_hook=nested_post_append_hook,
        book=book,
        authority=authority,
        paper_execution=execution,
    )
    loop.register_input("input-a", selection_ids="selection-a")

    def forged_verified_origin(*args, **kwargs):
        forged_origin_calls.append("called")
        raise AssertionError("mutable verified-origin alias must not be authority")

    monkeypatch.setattr(
        instance_guard,
        "_verified_decision_origin_without_instance_dispatch",
        forged_verified_origin,
    )
    result = loop.run_cycle()

    assert forged_origin_calls == []
    assert result.status is LiveCycleStatus.DECIDED
    assert result.decision_id is not None
    assert nested_rejections == [result.decision_id]
    assert result.paper_execution_run_id is not None
    decision_origin = origin_module.verified_decision_origin(
        JsonlDecisionLedger(tmp_path / "decisions.jsonl"),
        result.decision_id,
    )
    assert execution_ledger.reservation_decision_origin(
        result.paper_execution_run_id
    ) == decision_origin

    events = execution_ledger.events(result.paper_execution_run_id)
    event_types = [item["event_type"] for item in events]
    assert event_types[0] == "RUN_RESERVED"
    assert "ATTEMPT_RECORDED" in event_types
    assert event_types.index("RUN_RESERVED") < event_types.index("ATTEMPT_RECORDED")

    reservation = events[0]["payload"]
    assert captured_execution_plan
    with pytest.raises(
        PaperExecutionStateError,
        match="requires verified decision origin on resume",
    ):
        execution_ledger.load_run(
            run_id=result.paper_execution_run_id,
            trigger_id=result.decision_id,
            plan=captured_execution_plan[0],
            config=model,
            started_at=reservation["started_at"],
            observation_evidence_ids=reservation["observation_evidence_ids"],
        )


def _paper_value_goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-paper-value-origin",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("0.10"),
        max_capital_at_risk_fraction=Decimal("0.50"),
        max_risk_of_ruin=Decimal("1"),
        max_concurrent_positions=2,
        max_quote_age_seconds=Decimal("5"),
        minimum_data_quality=Decimal("0"),
    )


def _paper_value_event() -> MarketEvent:
    return MarketEvent(
        event_id="event-origin-paper-value",
        market_id="market-origin-paper-value",
        selection_id="selection-origin-paper-value",
        decimal_odds=Decimal("2"),
        observed_ts="2026-09-17T15:00:00+00:00",
        source_id="provider-origin-paper-value",
        sequence=1,
        source_ts="2026-09-17T14:59:59+00:00",
        ingest_ts="2026-09-17T15:00:00+00:00",
    )


def _paper_value_agent(event: MarketEvent, goal: EconomicGoalContract) -> PaperValueAgent:
    forecast = Forecast(
        quote_key=event.quote_key,
        probability=Decimal("0.60"),
        model_id="model-origin-paper-value",
        as_of_ts="2026-09-17T14:59:58+00:00",
    )
    return PaperValueAgent(
        {event.quote_key: forecast},
        stake=Decimal("1"),
        risk_policy=PaperRiskPolicy(economic_goal=goal),
    )


def _paper_value_context(
    tmp_path,
    *,
    event: MarketEvent,
    replay_run_id: str,
    ledger: JsonlDecisionLedger,
    book: PaperBook | None = None,
    paper_book_path=None,
) -> AgentContext:
    """Bind PaperValue tests to the same explicit #623 workspace authority as product code."""

    active_book = PaperBook("100") if book is None else book
    execution_book_path = (
        tmp_path / "paper-execution-book.json"
        if paper_book_path is None
        else paper_book_path
    )
    runtime = PaperExecutionAdoptionRuntime(
        book=active_book,
        ledger=PaperExecutionLedger(tmp_path / "paper-execution.jsonl"),
        config=_execution_model(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=execution_book_path,
    )
    return AgentContext(
        active_book,
        latest_quotes={event.quote_key: event},
        replay_run_id=replay_run_id,
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=((event.source_id, "test-account-1"),),
    )


def test_paper_value_rejects_shadowed_material_action_identity(tmp_path) -> None:
    goal = _paper_value_goal()
    event = _paper_value_event()
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    context = _paper_value_context(
        tmp_path,
        event=event,
        replay_run_id="run-origin-paper-value-shadow",
        ledger=ledger,
    )
    agent = _paper_value_agent(event, goal)
    attacker_called = False

    def forged_material_action_id(*args, **kwargs):
        nonlocal attacker_called
        attacker_called = True
        return "attacker-selected-decision"

    agent._material_action_id = forged_material_action_id

    with pytest.raises(
        origin_module.PaperExecutionDecisionOriginError,
        match="shadows authority method _material_action_id",
    ):
        agent.on_market_event(event, context)

    assert attacker_called is False
    assert not ledger.path.exists()
    runtime = context.paper_execution
    assert isinstance(runtime, PaperExecutionAdoptionRuntime)
    assert runtime.ledger.events() == ()


def test_paper_value_fresh_and_durable_recovery_keep_exact_origin(
    tmp_path,
    monkeypatch,
) -> None:
    goal = _paper_value_goal()
    event = _paper_value_event()
    ledger_path = tmp_path / "decisions.jsonl"
    book_path = tmp_path / "paper-restart.json"
    ledger = JsonlDecisionLedger(ledger_path)
    book = PaperBook("100")
    context = _paper_value_context(
        tmp_path,
        event=event,
        replay_run_id="run-origin-paper-value",
        ledger=ledger,
        book=book,
        paper_book_path=book_path,
    )
    agent = _paper_value_agent(event, goal)
    forged_origin_calls: list[str] = []

    def forged_verified_origin(*args, **kwargs):
        forged_origin_calls.append("called")
        raise AssertionError("mutable verified-origin alias must not be authority")

    monkeypatch.setattr(
        instance_guard,
        "_verified_decision_origin_without_instance_dispatch",
        forged_verified_origin,
    )

    agent.on_market_event(event, context)

    records = ledger.verified_records()
    assert len(records) == 1
    record = records[0]
    run_id = record.payload["execution_run_id"]
    assert isinstance(run_id, str) and run_id
    runtime = context.paper_execution
    assert isinstance(runtime, PaperExecutionAdoptionRuntime)
    exact_origin = origin_module.verified_decision_origin(ledger, record.decision_id)
    assert runtime.ledger.reservation_decision_origin(run_id) == exact_origin
    fresh_events = runtime.ledger.events(run_id)
    fresh_types = [item["event_type"] for item in fresh_events]
    assert fresh_types[0] == "RUN_RESERVED"
    assert "ATTEMPT_RECORDED" in fresh_types
    assert fresh_types.index("RUN_RESERVED") < fresh_types.index("ATTEMPT_RECORDED")

    book.save(book_path)
    restarted_book = PaperBook.load(book_path)
    restarted_context = _paper_value_context(
        tmp_path,
        event=event,
        replay_run_id="run-origin-paper-value",
        ledger=JsonlDecisionLedger(ledger_path),
        book=restarted_book,
        paper_book_path=book_path,
    )
    restarted_agent = _paper_value_agent(event, goal)
    restarted_runtime = restarted_context.paper_execution
    assert isinstance(restarted_runtime, PaperExecutionAdoptionRuntime)

    restarted_agent.on_market_event(event, restarted_context)

    assert forged_origin_calls == []
    assert event.quote_key in restarted_agent._acted
    assert restarted_runtime.ledger.reservation_decision_origin(run_id) == exact_origin
    assert restarted_runtime.ledger.events(run_id) == fresh_events
