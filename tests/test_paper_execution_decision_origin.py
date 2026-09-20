from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport import _paper_execution_decision_origin as origin_module
from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
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


def test_verified_decision_origin_is_persisted_before_attempts_and_restarts_exactly(
    tmp_path,
) -> None:
    decision_ledger = JsonlDecisionLedger(tmp_path / "decision.jsonl")
    digest = decision_ledger.append(_record())
    origin = origin_module.verified_decision_origin(decision_ledger, DECISION_ID)
    assert origin.record_sha256 == digest

    execution_ledger = PaperExecutionLedger(tmp_path / "paper-execution.jsonl")
    plan = _plan()
    token = origin_module._DECISION_ORIGIN.set(origin)
    try:
        _reserve(execution_ledger, plan)
        run = _load(execution_ledger, plan)
        _reserve(execution_ledger, plan)
        retry = _load(execution_ledger, plan)
    finally:
        origin_module._DECISION_ORIGIN.reset(token)

    assert run == retry
    assert execution_ledger.reservation_decision_origin(
        "paper-run-origin-test-1"
    ) == origin
    events = execution_ledger.events("paper-run-origin-test-1")
    assert events[0]["event_type"] == "RUN_RESERVED"
    assert events[0]["payload"]["decision_origin"] == origin.to_dict()
    assert all(event["event_type"] != "ATTEMPT_RECORDED" for event in events)


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
    token = origin_module._DECISION_ORIGIN.set(original)
    try:
        _reserve(execution_ledger, plan)
    finally:
        origin_module._DECISION_ORIGIN.reset(token)

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


def test_caller_shaped_context_frame_cannot_mint_product_origin(tmp_path) -> None:
    decision_ledger = JsonlDecisionLedger(tmp_path / "decision.jsonl")
    decision_ledger.append(_record())
    runtime = object()
    context = SimpleNamespace(
        paper_execution=runtime,
        decision_ledger=decision_ledger,
    )

    def attacker_frame():
        # Keep an exact-looking `context` local on the stack. Product origin must
        # still require an integrated producer's exact code object.
        assert context.paper_execution is runtime
        return origin_module._resolve_product_origin_from_stack(runtime, DECISION_ID)

    assert attacker_frame() is None
