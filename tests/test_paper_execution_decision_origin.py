from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport import _paper_execution_decision_origin as origin_module
from autosport import _paper_execution_decision_origin_instance_guard as instance_guard
from autosport import _paper_execution_decision_origin_resume_guard as resume_guard
from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


DECISION_ID = "decision-origin-test-1"
STARTED_AT = "2026-09-20T12:00:01+00:00"


def _plan(decision_id: str = DECISION_ID) -> ExecutionPlan:
    action = ExecutionAction(
        action_id="action-origin-test-1",
        bookmaker_id="paper-provider",
        account_id="paper-account",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds=Decimal("2.10"),
        requested_stake=Decimal("5.00"),
        quote_id="quote-1",
        quote_observed_at="2026-09-20T12:00:00+00:00",
        expires_at="2026-09-20T12:01:00+00:00",
    )
    return ExecutionPlan(
        plan_id="plan-origin-test-1",
        bookmaker_profile_version="paper-origin-test-v1",
        decision_id=decision_id,
        approval_id="paper-only",
        created_at=STARTED_AT,
        actions=(action,),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="origin-test-model",
        model_version="v1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="focused-origin-test",
        seed="origin-test-seed",
        max_quote_age_ms=60_000,
    )


def _record(decision_id: str = DECISION_ID, *, payload_value: str = "alpha") -> DecisionRecord:
    return DecisionRecord(
        replay_run_id="origin-test-run",
        agent="origin-test-agent",
        observed_ts=STARTED_AT,
        action="PAPER_TEST",
        payload={"value": payload_value},
        context_hash="context-origin-test",
        decision_id=decision_id,
        recorded_at="2026-09-20T12:00:00.500000+00:00",
    )


def _reserve(ledger: PaperExecutionLedger, plan: ExecutionPlan) -> None:
    ledger.reserve_run(
        run_id="paper-run-origin-test-1",
        trigger_id=plan.decision_id,
        plan=plan,
        config=_config(),
        started_at=STARTED_AT,
        observation_evidence_ids={},
    )


def _load(ledger: PaperExecutionLedger, plan: ExecutionPlan):
    return ledger.load_run(
        run_id="paper-run-origin-test-1",
        trigger_id=plan.decision_id,
        plan=plan,
        config=_config(),
        started_at=STARTED_AT,
        observation_evidence_ids={},
    )


def _append_origin_reservation_for_test(
    ledger: PaperExecutionLedger,
    plan: ExecutionPlan,
    origin: origin_module.DecisionRecordOrigin,
) -> None:
    config = _config()
    instance_guard._STABLE_APPEND_EVENT(
        ledger,
        event_type="RUN_RESERVED",
        run_id="paper-run-origin-test-1",
        key="paper-run-origin-test-1:reserve",
        payload={
            "trigger_id": plan.decision_id,
            "plan_id": plan.plan_id,
            "plan_fingerprint": plan.fingerprint,
            "model_fingerprint": config.fingerprint,
            "started_at": STARTED_AT,
            "action_ids": [action.action_id for action in plan.actions],
            "observation_evidence_ids": {},
            "decision_origin": origin.to_dict(),
        },
    )


def test_verified_origin_is_evidence_not_direct_reservation_capability(tmp_path) -> None:
    decision_ledger = JsonlDecisionLedger(tmp_path / "decision.jsonl")
    digest = decision_ledger.append(_record())
    origin = origin_module.verified_decision_origin(decision_ledger, DECISION_ID)
    assert origin.record_sha256 == digest

    execution_ledger = PaperExecutionLedger(tmp_path / "paper-execution.jsonl")
    token = origin_module._DECISION_ORIGIN.set(origin)
    try:
        with pytest.raises(
            origin_module.PaperExecutionDecisionOriginError,
            match="canonical product execution",
        ):
            _reserve(execution_ledger, _plan())
    finally:
        origin_module._DECISION_ORIGIN.reset(token)

    assert execution_ledger.events("paper-run-origin-test-1") == ()


def test_verified_origin_cannot_smuggle_through_raw_execute_paper_plan(tmp_path) -> None:
    decision_ledger = JsonlDecisionLedger(tmp_path / "decision.jsonl")
    decision_ledger.append(_record())
    origin = origin_module.verified_decision_origin(decision_ledger, DECISION_ID)
    execution_ledger = PaperExecutionLedger(tmp_path / "paper-execution.jsonl")
    plan = _plan()

    token = origin_module._DECISION_ORIGIN.set(origin)
    try:
        with pytest.raises(
            origin_module.PaperExecutionDecisionOriginError,
            match="canonical product execution",
        ):
            execute_paper_plan(
                plan=plan,
                trigger_id=DECISION_ID,
                config=_config(),
                ledger=execution_ledger,
                started_at=STARTED_AT,
            )
    finally:
        origin_module._DECISION_ORIGIN.reset(token)

    assert execution_ledger.events("paper-run-origin-test-1") == ()


def test_originless_reservation_cannot_be_upgraded_by_late_decision_append(tmp_path) -> None:
    execution_ledger = PaperExecutionLedger(tmp_path / "paper-execution.jsonl")
    plan = _plan()
    _reserve(execution_ledger, plan)
    assert execution_ledger.reservation_decision_origin("paper-run-origin-test-1") is None

    decision_ledger = JsonlDecisionLedger(tmp_path / "decision.jsonl")
    decision_ledger.append(_record())
    late_origin = origin_module.verified_decision_origin(decision_ledger, DECISION_ID)

    token = origin_module._DECISION_ORIGIN.set(late_origin)
    try:
        with pytest.raises(PaperExecutionStateError, match="lacks pre-execution decision origin"):
            _load(execution_ledger, plan)
    finally:
        origin_module._DECISION_ORIGIN.reset(token)


def test_changed_record_digest_cannot_resume_same_execution_reservation(tmp_path) -> None:
    decision_ledger = JsonlDecisionLedger(tmp_path / "decision.jsonl")
    decision_ledger.append(_record())
    original = origin_module.verified_decision_origin(decision_ledger, DECISION_ID)

    execution_ledger = PaperExecutionLedger(tmp_path / "paper-execution.jsonl")
    plan = _plan()
    _append_origin_reservation_for_test(execution_ledger, plan, original)

    substituted = origin_module.DecisionRecordOrigin(
        decision_id=DECISION_ID,
        record_sha256="b" * 64,
    )
    assert substituted != original
    token = origin_module._DECISION_ORIGIN.set(substituted)
    try:
        with pytest.raises(PaperExecutionStateError, match="changed across retry/restart"):
            _load(execution_ledger, plan)
    finally:
        origin_module._DECISION_ORIGIN.reset(token)


def test_decision_origin_rejects_polymorphic_ledger_authority(tmp_path) -> None:
    class ForgedDecisionLedger(JsonlDecisionLedger):
        pass

    forged = ForgedDecisionLedger(tmp_path / "forged.jsonl")
    with pytest.raises(
        origin_module.PaperExecutionDecisionOriginError,
        match="exact JsonlDecisionLedger",
    ):
        origin_module.verified_decision_origin(forged, DECISION_ID)


def test_origin_bound_reservation_rejects_polymorphic_execution_ledger(tmp_path) -> None:
    class ForgedExecutionLedger(PaperExecutionLedger):
        pass

    decision_ledger = JsonlDecisionLedger(tmp_path / "decision.jsonl")
    decision_ledger.append(_record())
    origin = origin_module.verified_decision_origin(decision_ledger, DECISION_ID)
    forged = ForgedExecutionLedger(tmp_path / "forged-execution.jsonl")

    token = origin_module._DECISION_ORIGIN.set(origin)
    try:
        with pytest.raises(
            origin_module.PaperExecutionDecisionOriginError,
            match="exact PaperExecutionLedger",
        ):
            _reserve(forged, _plan())
    finally:
        origin_module._DECISION_ORIGIN.reset(token)


def test_exact_decision_ledger_method_shadow_cannot_mint_origin(tmp_path) -> None:
    ledger = JsonlDecisionLedger(tmp_path / "decision.jsonl")
    ledger.append(_record())
    attacker_called = False

    def forged_verified_snapshot():
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("shadowed verified_snapshot must not execute")

    ledger.verified_snapshot = forged_verified_snapshot  # type: ignore[method-assign]
    with pytest.raises(
        origin_module.PaperExecutionDecisionOriginError,
        match="shadows authority method verified_snapshot",
    ):
        origin_module.verified_decision_origin(ledger, DECISION_ID)
    assert attacker_called is False


def test_exact_execution_ledger_method_shadow_cannot_write_origin(tmp_path) -> None:
    decision_ledger = JsonlDecisionLedger(tmp_path / "decision.jsonl")
    decision_ledger.append(_record())
    origin = origin_module.verified_decision_origin(decision_ledger, DECISION_ID)
    ledger = PaperExecutionLedger(tmp_path / "paper-execution.jsonl")
    attacker_called = False

    def forged_append_event(**kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("shadowed _append_event must not execute")

    ledger._append_event = forged_append_event  # type: ignore[method-assign]
    token = origin_module._DECISION_ORIGIN.set(origin)
    try:
        with pytest.raises(
            origin_module.PaperExecutionDecisionOriginError,
            match="shadows authority method _append_event",
        ):
            _reserve(ledger, _plan())
    finally:
        origin_module._DECISION_ORIGIN.reset(token)
    assert attacker_called is False


def test_caller_shaped_context_frame_cannot_mint_product_origin(tmp_path) -> None:
    decision_ledger = JsonlDecisionLedger(tmp_path / "decision.jsonl")
    decision_ledger.append(_record())
    runtime = object()
    context = SimpleNamespace(
        paper_execution=runtime,
        decision_ledger=decision_ledger,
    )

    def attacker_frame():
        assert context.paper_execution is runtime
        return origin_module._resolve_product_origin_from_stack(runtime, DECISION_ID)

    assert attacker_frame() is None


def test_mutable_verified_snapshot_alias_cannot_mint_origin(tmp_path, monkeypatch) -> None:
    ledger = JsonlDecisionLedger(tmp_path / "sealed-decision.jsonl")
    durable_digest = ledger.append(_record())
    attacker_called = False

    def forged_verified_snapshot(_ledger):
        nonlocal attacker_called
        attacker_called = True
        payload = (
            '{"record":{"decision_id":"decision-origin-test-1"},'
            '"sha256":"' + ("f" * 64) + '"}\n'
        ).encode("utf-8")
        return SimpleNamespace(payload=payload)

    monkeypatch.setattr(instance_guard, "_STABLE_VERIFIED_SNAPSHOT", forged_verified_snapshot)
    origin = origin_module.verified_decision_origin(ledger, DECISION_ID)

    assert attacker_called is False
    assert origin.record_sha256 == durable_digest


def test_mutable_events_alias_cannot_forge_reservation_origin(tmp_path, monkeypatch) -> None:
    ledger = PaperExecutionLedger(tmp_path / "sealed-events.jsonl")
    attacker_called = False
    forged_origin = origin_module.DecisionRecordOrigin(
        decision_id=DECISION_ID,
        record_sha256="e" * 64,
    )

    def forged_events(_ledger, _run_id=None):
        nonlocal attacker_called
        attacker_called = True
        return (
            {
                "event_type": "RUN_RESERVED",
                "payload": {"decision_origin": forged_origin.to_dict()},
            },
        )

    monkeypatch.setattr(resume_guard, "_STABLE_EVENTS", forged_events)

    assert ledger.reservation_decision_origin("never-reserved") is None
    assert attacker_called is False
