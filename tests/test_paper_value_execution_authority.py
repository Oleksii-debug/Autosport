from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from autosport import _paper_value_execution_authority as paper_value_authority
from autosport.agents import AgentContext
from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
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


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-paper-value-authority",
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


def test_caller_authored_economic_restart_record_cannot_be_first_execution_authority(
    tmp_path,
) -> None:
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    event = _event()
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    goal = _goal()
    policy = PaperRiskPolicy(economic_goal=goal)
    agent = PaperValueAgent(
        {event.quote_key: _forecast(event)},
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )
    context = AgentContext(
        book,
        replay_run_id="replay-economic-forgery",
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
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
    )
    action = descriptor.execution_plan.actions[0]
    record = DecisionRecord(
        replay_run_id=context.replay_run_id,
        agent=agent.name,
        observed_ts=event.observed_ts,
        action="OPEN_PAPER_VALUE_TICKET",
        payload={
            "quote_key": event.quote_key,
            "forecast_model": "caller-model",
            "probability": "0.99",
            "expected_profit_per_unit": "0.98",
            "stake": str(action.requested_stake),
            "requested_stake": str(action.requested_stake),
            "material_action_id": decision_id,
            "execution_plan_id": descriptor.execution_plan.plan_id,
            "execution_plan_fingerprint": descriptor.execution_plan.fingerprint,
            "execution_run_id": runtime.expected_run_id(descriptor, decision_id),
            "execution_authority_json": descriptor.intent_evidence_json,
        },
        context_hash=context.market_context_hash(),
        decision_id=decision_id,
        decision_kind=ECONOMIC_DECISION_KIND,
    )
    ledger.append_economic(
        record,
        EconomicDecisionAuthority(goal, policy),
    )

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="durable ECONOMIC paper-value action lacks canonical risk admission witness",
    ):
        agent.on_market_event(event, context)

    assert book.balance == Decimal("100.00")
    assert not book.tickets
    assert not runtime.ledger.events()


def test_canonical_economic_agent_issues_origin_witness_before_execution(
    tmp_path,
) -> None:
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    event = _event()
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    goal = _goal()
    policy = PaperRiskPolicy(economic_goal=goal)
    agent = PaperValueAgent(
        {event.quote_key: _forecast(event)},
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )
    context = AgentContext(
        book,
        replay_run_id="replay-economic-canonical",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )

    agent.on_market_event(event, context)

    assert len(book.tickets) == 1
    witness_root = tmp_path / ".paper-value-risk-admissions"
    witnesses = tuple(
        path
        for path in witness_root.glob("*.json")
        if not path.name.endswith(".prepare.json")
        and not path.name.endswith(".pre-action.json")
    )
    assert len(witnesses) == 1
    payload = json.loads(witnesses[0].read_text(encoding="utf-8"))
    assert payload["schema"] == "autosport.paper_value.economic_risk_admission"
    assert payload["decision_kind"] == ECONOMIC_DECISION_KIND
    assert payload["risk_policy_sha256"] == policy.provenance_sha256

    durable_lines = ledger.verified_snapshot().payload.decode("utf-8").splitlines()
    envelopes = [json.loads(line) for line in durable_lines]
    matching = [
        envelope
        for envelope in envelopes
        if envelope["record"]["decision_id"] == payload["decision_id"]
    ]
    assert len(matching) == 1
    assert payload["decision_record_sha256"] == matching[0]["sha256"]


def _crash_economic_after_origin_witness_before_reservation(
    tmp_path,
    monkeypatch,
):
    book = PaperBook("100.00")
    runtime = _runtime(tmp_path, book)
    event = _event()
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    goal = _goal()
    policy = PaperRiskPolicy(economic_goal=goal)
    agent = PaperValueAgent(
        {event.quote_key: _forecast(event)},
        stake=Decimal("1.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=policy,
    )
    context = AgentContext(
        book,
        replay_run_id="replay-economic-crash-window",
        decision_ledger=ledger,
        paper_execution=runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
    original_execute = paper_value_authority._ORIGINAL_EXECUTE

    def injected_crash(*_args, **_kwargs):
        raise RuntimeError("injected crash before #623 reservation")

    monkeypatch.setattr(
        paper_value_authority,
        "_ORIGINAL_EXECUTE",
        injected_crash,
    )
    with pytest.raises(
        RuntimeError,
        match="injected crash before #623 reservation",
    ):
        agent.on_market_event(event, context)

    # Authorization ran before the injected base-execution crash, so both the
    # durable decision and origin witness exist, but no #623 reservation exists.
    assert len(ledger.verified_records()) == 1
    assert not runtime.ledger.events()
    witness_root = tmp_path / ".paper-value-risk-admissions"
    witnesses = tuple(
        path
        for path in witness_root.glob("*.json")
        if not path.name.endswith(".prepare.json")
        and not path.name.endswith(".pre-action.json")
    )
    assert len(witnesses) == 1
    monkeypatch.setattr(
        paper_value_authority,
        "_ORIGINAL_EXECUTE",
        original_execute,
    )
    return event, ledger, goal, policy, witnesses[0]


def test_economic_origin_witness_recovers_exact_pre_reservation_crash(
    tmp_path,
    monkeypatch,
) -> None:
    event, ledger, goal, policy, witness = (
        _crash_economic_after_origin_witness_before_reservation(
            tmp_path,
            monkeypatch,
        )
    )
    witness_payload = json.loads(witness.read_text(encoding="utf-8"))

    restarted_book = PaperBook("100.00")
    restarted_runtime = _runtime(tmp_path, restarted_book)
    restarted_context = AgentContext(
        restarted_book,
        replay_run_id="replay-economic-crash-window",
        decision_ledger=ledger,
        paper_execution=restarted_runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
    recovering = PaperValueAgent(
        {},
        stake=Decimal("99.00"),
        minimum_expected_profit_per_unit=Decimal("999"),
        risk_policy=policy,
    )

    # Recovery intentionally occurs before fresh forecast/edge/sizing proposal
    # gates and must execute exactly the already-durable decision.
    recovering.on_market_event(event, restarted_context)

    assert len(restarted_book.tickets) == 1
    ticket = next(iter(restarted_book.tickets.values()))
    assert ticket.stake == Decimal(witness_payload["requested_stake"])
    assert any(
        item.get("event_type") == "RUN_RESERVED"
        for item in restarted_runtime.ledger.events()
    )
    record = ledger.verified_records()[0]
    envelopes = [
        json.loads(line)
        for line in ledger.verified_snapshot().payload.decode("utf-8").splitlines()
    ]
    matching = [
        envelope
        for envelope in envelopes
        if envelope["record"]["decision_id"] == record.decision_id
    ]
    assert len(matching) == 1
    assert witness_payload["decision_record_sha256"] == matching[0]["sha256"]
    assert goal == policy.economic_goal


def test_economic_origin_witness_coherent_tamper_fails_closed_in_crash_window(
    tmp_path,
    monkeypatch,
) -> None:
    event, ledger, _, policy, witness = (
        _crash_economic_after_origin_witness_before_reservation(
            tmp_path,
            monkeypatch,
        )
    )
    payload = json.loads(witness.read_text(encoding="utf-8"))
    payload["pre_action_book_sha256"] = "0" * 64
    unsigned = dict(payload)
    unsigned.pop("witness_sha256")
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    payload["witness_sha256"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    witness.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    restarted_book = PaperBook("100.00")
    restarted_runtime = _runtime(tmp_path, restarted_book)
    restarted_context = AgentContext(
        restarted_book,
        replay_run_id="replay-economic-crash-window",
        decision_ledger=ledger,
        paper_execution=restarted_runtime,
        paper_provider_accounts=(("provider-a", "account-a"),),
    )
    recovering = PaperValueAgent(
        {},
        stake=Decimal("99.00"),
        minimum_expected_profit_per_unit=Decimal("999"),
        risk_policy=policy,
    )

    with pytest.raises(
        PaperExecutionAdoptionError,
        match="risk admission binding changed across restart",
    ):
        recovering.on_market_event(event, restarted_context)

    assert restarted_book.balance == Decimal("100.00")
    assert not restarted_book.tickets
    assert not restarted_runtime.ledger.events()


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
