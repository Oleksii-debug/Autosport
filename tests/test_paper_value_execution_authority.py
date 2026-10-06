from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from autosport.agents import AgentContext
from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.paper_strategy import Forecast, PaperValueAgent
from autosport.risk import PaperRiskPolicy


_GENERAL_RECOVERY_ERROR = (
    "durable GENERAL paper-value action lacks canonical risk admission witness"
)


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-value-authority-test",
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


def _forecast(event: MarketEvent, probability: str = "0.75") -> Forecast:
    return Forecast(
        quote_key=event.quote_key,
        probability=Decimal(probability),
        model_id="model-a",
        as_of_ts=event.observed_ts,
    )


def _executed_general_action(tmp_path):
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    event = _event()
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    policy = PaperRiskPolicy(max_ticket_fraction=Decimal("0.02"))
    agent = PaperValueAgent(
        {event.quote_key: _forecast(event)},
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )
    context = AgentContext(
        book,
        replay_run_id="replay-a",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
    agent.on_market_event(event, context)
    assert len(book.tickets) == 1
    # Same-process duplicate delivery is already proven complete by the canonical
    # agent instance and must remain a no-op rather than entering restart recovery.
    agent.on_market_event(event, context)
    assert len(book.tickets) == 1
    return book, runtime, event, ledger, policy, context


def _restarted_general_context(tmp_path, policy: PaperRiskPolicy):
    book = PaperBook.load(tmp_path / "paper-book.json")
    runtime = _runtime(tmp_path, book)
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    context = AgentContext(
        book,
        replay_run_id="replay-a",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
    return book, runtime, context


def test_public_paper_value_prepare_is_descriptor_only(tmp_path) -> None:
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    event = _event()

    descriptor = runtime.prepare_paper_value_action(
        event=event,
        stake=Decimal("10.00"),
        decision_id="decision-a",
        account_id="account-a",
        bankroll_id="bankroll-a",
        currency="EUR",
    )

    assert descriptor.__class__.__name__ == "PaperValueExecutionDescriptor"
    assert runtime._prepared_authorities == {}

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="requires the canonical PaperValueAgent execution path",
    ):
        runtime.execute(
            prepared=descriptor,
            trigger_id="decision-a",
            started_at=event.observed_ts,
            materialize_exposure=True,
        )

    assert book.balance == Decimal("100.00")
    assert not book.tickets


def test_caller_authored_general_decision_and_ambient_context_cannot_authorize(
    tmp_path,
) -> None:
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    event = _event()
    decision_id = "forged-decision-a"
    descriptor = runtime.prepare_paper_value_action(
        event=event,
        stake=Decimal("10.00"),
        decision_id=decision_id,
        account_id="account-a",
        bankroll_id=None,
        currency=None,
    )
    action = descriptor.execution_plan.actions[0]
    ledger = JsonlDecisionLedger(tmp_path / "forged-decisions.jsonl")
    ledger.append(
        DecisionRecord(
            replay_run_id="forged-run",
            agent=PaperValueAgent.name,
            observed_ts=event.observed_ts,
            action="OPEN_PAPER_VALUE_TICKET",
            payload={
                "quote_key": event.quote_key,
                "material_action_id": decision_id,
                "requested_stake": str(action.requested_stake),
                "execution_plan_id": descriptor.execution_plan.plan_id,
                "execution_plan_fingerprint": descriptor.execution_plan.fingerprint,
                "execution_run_id": runtime.expected_run_id(descriptor, decision_id),
                "execution_authority_json": descriptor.intent_evidence_json,
            },
            context_hash="caller-authored",
            decision_id=decision_id,
        )
    )

    runtime._paper_value_decision_context = (
        ledger,
        None,
        PaperRiskPolicy(),
        "forged-run",
    )
    with pytest.raises(
        PaperExecutionAdoptionError,
        match="requires the canonical PaperValueAgent execution path",
    ):
        runtime.execute(
            prepared=descriptor,
            trigger_id=decision_id,
            started_at=event.observed_ts,
            materialize_exposure=True,
        )

    assert book.balance == Decimal("100.00")
    assert not book.tickets
    assert runtime._prepared_authorities == {}


def test_caller_authored_general_restart_record_cannot_be_first_execution_authority(
    tmp_path,
) -> None:
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    event = _event()
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    policy = PaperRiskPolicy(max_ticket_fraction=Decimal("0.02"))
    agent = PaperValueAgent(
        {event.quote_key: _forecast(event)},
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )
    context = AgentContext(
        book,
        replay_run_id="replay-a",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
    decision_id = agent._material_action_id(context, event)
    descriptor = runtime.prepare_paper_value_action(
        event=event,
        stake=Decimal("1.00"),
        decision_id=decision_id,
        account_id="account-a",
        bankroll_id=None,
        currency=None,
    )
    ledger.append(
        DecisionRecord(
            replay_run_id=context.replay_run_id,
            agent=agent.name,
            observed_ts=event.observed_ts,
            action="OPEN_PAPER_VALUE_TICKET",
            payload={
                "quote_key": event.quote_key,
                "forecast_model": "model-a",
                "probability": "0.75",
                "expected_profit_per_unit": "0.50",
                "stake": "1.00",
                "requested_stake": "1.00",
                "material_action_id": decision_id,
                "execution_plan_id": descriptor.execution_plan.plan_id,
                "execution_plan_fingerprint": descriptor.execution_plan.fingerprint,
                "execution_run_id": runtime.expected_run_id(descriptor, decision_id),
                "execution_authority_json": descriptor.intent_evidence_json,
            },
            context_hash=context.market_context_hash(),
            decision_id=decision_id,
        )
    )

    with pytest.raises(PaperExecutionAdoptionError, match=_GENERAL_RECOVERY_ERROR):
        agent.on_market_event(event, context)

    assert book.balance == Decimal("100.00")
    assert not book.tickets
    assert not runtime.ledger.events()


def test_canonical_goal_less_agent_path_still_executes_after_risk_pass(tmp_path) -> None:
    book, runtime, event, _, _, _ = _executed_general_action(tmp_path)

    ticket = next(iter(book.tickets.values()))
    assert ticket.stake == Decimal("1.00")
    assert book.balance == Decimal("99.00")
    assert any(
        item.get("event_type") == "RUN_RESERVED"
        for item in runtime.ledger.events()
    )
    assert event.quote_key


def test_durable_general_action_restarts_before_missing_forecast_gate(tmp_path) -> None:
    original_book, _, event, _, policy, _ = _executed_general_action(tmp_path)
    balance = original_book.balance
    ticket_ids = tuple(original_book.tickets)
    book, runtime, context = _restarted_general_context(tmp_path, policy)
    recovering = PaperValueAgent(
        {},
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )

    recovering.on_market_event(event, context)

    assert book.balance == balance
    assert tuple(book.tickets) == ticket_ids
    assert len(book.tickets) == 1
    assert any(item.get("event_type") == "RUN_RESERVED" for item in runtime.ledger.events())


def test_durable_general_action_restarts_before_changed_edge_gate(tmp_path) -> None:
    original_book, _, event, _, policy, _ = _executed_general_action(tmp_path)
    balance = original_book.balance
    ticket_ids = tuple(original_book.tickets)
    book, _, context = _restarted_general_context(tmp_path, policy)
    recovering = PaperValueAgent(
        {event.quote_key: _forecast(event, probability="0.40")},
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0.50"),
        risk_policy=policy,
    )

    recovering.on_market_event(event, context)

    assert book.balance == balance
    assert tuple(book.tickets) == ticket_ids
    assert len(book.tickets) == 1


def test_durable_general_action_restarts_before_closed_status_gate(tmp_path) -> None:
    original_book, _, event, _, policy, _ = _executed_general_action(tmp_path)
    balance = original_book.balance
    ticket_ids = tuple(original_book.tickets)
    book, _, context = _restarted_general_context(tmp_path, policy)
    closed_payload = event.to_dict()
    closed_payload["status"] = "closed"
    closed_event = MarketEvent.from_dict(closed_payload)
    assert closed_event.quote_key == event.quote_key
    recovering = PaperValueAgent(
        {},
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0.99"),
        risk_policy=policy,
    )

    recovering.on_market_event(closed_event, context)

    assert book.balance == balance
    assert tuple(book.tickets) == ticket_ids
    assert len(book.tickets) == 1


def test_tampered_general_risk_admission_fails_closed_on_restart(tmp_path) -> None:
    original_book, _, event, _, policy, _ = _executed_general_action(tmp_path)
    witness_root = tmp_path / ".paper-value-risk-admissions"
    witnesses = tuple(
        path
        for path in witness_root.glob("*.json")
        if not path.name.endswith(".prepare.json")
        and not path.name.endswith(".pre-action.json")
    )
    assert len(witnesses) == 1
    witness = witnesses[0]
    payload = json.loads(witness.read_text(encoding="utf-8"))
    payload["risk_policy_sha256"] = "0" * 64
    witness.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    book, _, context = _restarted_general_context(tmp_path, policy)
    recovering = PaperValueAgent(
        {},
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )
    with pytest.raises(
        PaperExecutionAdoptionError,
        match="risk admission witness digest mismatch",
    ):
        recovering.on_market_event(event, context)

    assert book.balance == original_book.balance
    assert tuple(book.tickets) == tuple(original_book.tickets)
