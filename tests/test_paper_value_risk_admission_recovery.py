from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

import autosport._paper_value_risk_admission_recovery as recovery
from autosport.agents import AgentContext
from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionError, PaperExecutionAdoptionRuntime
from autosport.paper_execution_reality import EvidenceGrade, PaperExecutionLedger, PaperExecutionModelConfig
from autosport.paper_strategy import Forecast, PaperValueAgent
from autosport.risk import PaperRiskPolicy


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="risk-admission-recovery-test",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="test-seeded-model",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _runtime(tmp_path, book: PaperBook) -> PaperExecutionAdoptionRuntime:
    return PaperExecutionAdoptionRuntime(
        book=book,
        ledger=PaperExecutionLedger(tmp_path / "paper-execution.jsonl"),
        config=_config(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=tmp_path / "paper-book.json",
    )


def _event() -> MarketEvent:
    return MarketEvent(
        event_id="event-a",
        market_id="market-a",
        selection_id="selection-a",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-20T09:00:00+00:00",
        source_id="provider-a",
        sequence=1,
        sport="football",
    )


def _agent(event: MarketEvent, policy: PaperRiskPolicy, *, forecasts: bool = True) -> PaperValueAgent:
    values = (
        {
            event.quote_key: Forecast(
                quote_key=event.quote_key,
                probability=Decimal("0.75"),
                model_id="model-a",
                as_of_ts=event.observed_ts,
            )
        }
        if forecasts
        else {}
    )
    return PaperValueAgent(
        values,
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )


def _context(tmp_path, book: PaperBook, runtime: PaperExecutionAdoptionRuntime) -> AgentContext:
    return AgentContext(
        book,
        replay_run_id="replay-a",
        decision_ledger=JsonlDecisionLedger(tmp_path / "decisions.jsonl"),
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )


def _inject_pre_action_commit_crash(tmp_path, monkeypatch):
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    context = _context(tmp_path, book, runtime)
    event = _event()
    policy = PaperRiskPolicy(max_ticket_fraction=Decimal("0.02"))
    agent = _agent(event, policy)
    original_write_commit = recovery._write_commit

    def crash_before_commit(path, witness):
        raise RuntimeError("injected-risk-admission-crash")

    monkeypatch.setattr(recovery, "_write_commit", crash_before_commit)
    with pytest.raises(RuntimeError, match="injected-risk-admission-crash"):
        agent.on_market_event(event, context)
    monkeypatch.setattr(recovery, "_write_commit", original_write_commit)

    root = tmp_path / ".paper-value-risk-admissions"
    prepares = tuple(root.glob("*.prepare.json"))
    pre_actions = tuple(root.glob("*.pre-action.json"))
    commits = tuple(
        path
        for path in root.glob("*.json")
        if not path.name.endswith(".prepare.json")
        and not path.name.endswith(".pre-action.json")
    )
    assert len(prepares) == 1
    assert len(pre_actions) == 1
    assert commits == ()
    assert not runtime.ledger.events()
    assert not book.tickets
    assert len(tuple(context.decision_ledger.verified_records())) == 1
    return event, policy, pre_actions[0]


def test_general_risk_admission_recovers_crash_after_pre_action_before_commit(
    tmp_path, monkeypatch
) -> None:
    event, policy, pre_action_path = _inject_pre_action_commit_crash(tmp_path, monkeypatch)

    restarted_book = PaperBook.load(pre_action_path)
    restarted_runtime = _runtime(tmp_path, restarted_book)
    restarted_context = _context(tmp_path, restarted_book, restarted_runtime)
    recovering = _agent(event, policy, forecasts=False)

    recovering.on_market_event(event, restarted_context)

    assert len(restarted_book.tickets) == 1
    assert restarted_book.balance == Decimal("99.00")
    reserved = [
        item
        for item in restarted_runtime.ledger.events()
        if item.get("event_type") == "RUN_RESERVED"
    ]
    assert len(reserved) == 1
    root = tmp_path / ".paper-value-risk-admissions"
    commits = tuple(
        path
        for path in root.glob("*.json")
        if not path.name.endswith(".prepare.json")
        and not path.name.endswith(".pre-action.json")
    )
    assert len(commits) == 1

    second_book = PaperBook.load(tmp_path / "paper-book.json")
    second_runtime = _runtime(tmp_path, second_book)
    second_context = _context(tmp_path, second_book, second_runtime)
    _agent(event, policy, forecasts=False).on_market_event(event, second_context)

    assert len(second_book.tickets) == 1
    assert second_book.balance == Decimal("99.00")
    assert len(
        [
            item
            for item in second_runtime.ledger.events()
            if item.get("event_type") == "RUN_RESERVED"
        ]
    ) == 1


def test_general_risk_admission_prepare_rejects_policy_drift_after_crash(
    tmp_path, monkeypatch
) -> None:
    event, _, pre_action_path = _inject_pre_action_commit_crash(tmp_path, monkeypatch)
    changed_policy = PaperRiskPolicy(max_ticket_fraction=Decimal("0.03"))
    restarted_book = PaperBook.load(pre_action_path)
    restarted_runtime = _runtime(tmp_path, restarted_book)
    restarted_context = _context(tmp_path, restarted_book, restarted_runtime)

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="PREPARE binding changed across restart",
    ):
        _agent(event, changed_policy, forecasts=False).on_market_event(
            event, restarted_context
        )

    assert not restarted_runtime.ledger.events()
    assert not restarted_book.tickets


def test_general_recovery_prepare_cannot_bypass_exact_pre_action_risk_gate(tmp_path) -> None:
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    context = _context(tmp_path, book, runtime)
    event = _event()
    policy = PaperRiskPolicy(max_ticket_fraction=Decimal("0.02"))
    recovering = PaperValueAgent(
        {},
        stake=Decimal("10.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )
    decision_id = recovering._material_action_id(context, event)
    descriptor = runtime.prepare_paper_value_action(
        event=event,
        stake=Decimal("10.00"),
        decision_id=decision_id,
        account_id="account-a",
        bankroll_id=None,
        currency=None,
    )
    action = descriptor.execution_plan.actions[0]
    expected_run_id = runtime.expected_run_id(descriptor, decision_id)
    record = DecisionRecord(
        replay_run_id=context.replay_run_id,
        agent=PaperValueAgent.name,
        observed_ts=event.observed_ts,
        action="OPEN_PAPER_VALUE_TICKET",
        payload={
            "quote_key": event.quote_key,
            "material_action_id": decision_id,
            "requested_stake": str(action.requested_stake),
            "execution_plan_id": descriptor.execution_plan.plan_id,
            "execution_plan_fingerprint": descriptor.execution_plan.fingerprint,
            "execution_run_id": expected_run_id,
            "execution_authority_json": descriptor.intent_evidence_json,
        },
        context_hash=context.market_context_hash(),
        decision_id=decision_id,
    )
    context.decision_ledger.append(record)
    durable = tuple(context.decision_ledger.verified_records())
    assert durable == (record,)

    pre_action_sha256 = policy.risk_of_ruin_portfolio_sha256(book)
    assert pre_action_sha256 is not None
    witness = recovery._expected_witness(
        agent=recovering,
        record=record,
        descriptor=descriptor,
        expected_run_id=expected_run_id,
        pre_action_sha256=pre_action_sha256,
    )
    witness_path, pre_action_path = recovery._authority._risk_admission_paths(
        context.decision_ledger,
        decision_id,
    )
    witness_path.parent.mkdir(parents=True, exist_ok=True)
    book.save(pre_action_path)
    recovery.atomic_write_json(
        recovery._prepare_path(witness_path),
        recovery._prepare_payload(witness),
    )

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="no longer passes canonical risk evaluation",
    ):
        recovering.on_market_event(event, context)

    assert not witness_path.exists()
    assert not runtime.ledger.events()
    assert not book.tickets
    assert book.balance == Decimal("100.00")
