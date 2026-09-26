from __future__ import annotations

import importlib
from datetime import timedelta
from decimal import Decimal

from autosport import _paper_execution_decision_origin as origin_module
from autosport import _paper_execution_decision_origin_callsite_guard as callsite_guard
from autosport import _paper_execution_decision_origin_instance_guard as instance_guard
from autosport import _paper_execution_decision_origin_resume_guard as resume_guard
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


STARTED_AT = "2026-09-20T12:00:01+00:00"


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="origin-reload-model",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="origin-reload-test",
        seed="origin-reload-seed",
        max_quote_age_ms=60_000,
        min_delay_ms=0,
        max_delay_ms=0,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _plan(decision_id: str = "decision-origin-reload") -> ExecutionPlan:
    action = ExecutionAction(
        action_id="action-origin-reload",
        bookmaker_id="paper-provider",
        account_id="paper-account",
        event_id="event-origin-reload",
        market_id="market-origin-reload",
        selection_id="selection-origin-reload",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("1.00"),
        quote_id="quote-origin-reload",
        quote_observed_at="2026-09-20T12:00:00+00:00",
        expires_at="2026-09-20T12:02:00+00:00",
    )
    return ExecutionPlan(
        plan_id="plan-origin-reload",
        bookmaker_profile_version="paper-origin-reload-v1",
        decision_id=decision_id,
        approval_id="paper-only",
        created_at=STARTED_AT,
        actions=(action,),
    )


def test_repeated_origin_module_reload_preserves_originless_reserve_load_and_execute(
    tmp_path,
) -> None:
    importlib.reload(origin_module)
    importlib.reload(origin_module)

    try:
        config = _config()
        plan = _plan()
        ledger = PaperExecutionLedger(tmp_path / "paper-execution.jsonl")

        ledger.reserve_run(
            run_id="manual-originless-reload-run",
            trigger_id=plan.decision_id,
            plan=plan,
            config=config,
            started_at=STARTED_AT,
            observation_evidence_ids={},
        )
        manual = ledger.load_run(
            run_id="manual-originless-reload-run",
            trigger_id=plan.decision_id,
            plan=plan,
            config=config,
            started_at=STARTED_AT,
            observation_evidence_ids={},
        )
        assert manual is not None
        assert ledger.reservation_decision_origin("manual-originless-reload-run") is None

        runtime = PaperExecutionAdoptionRuntime(
            book=PaperBook("100"),
            ledger=ledger,
            config=config,
            max_quote_age=timedelta(seconds=60),
            paper_book_path=tmp_path / "paper-book.json",
        )
        action = plan.actions[0]
        prepared = runtime._mint_prepared(
            PreparedPaperExecution(
                execution_plan=plan,
                exposure_bindings=(
                    PaperExposureBinding(
                        action_id=action.action_id,
                        sport=None,
                        bankroll_id=None,
                        currency=None,
                    ),
                ),
                intent_evidence_json="{}",
            )
        )
        # White-box fixture: model an already-authorized lower-layer execution.
        runtime._exposure_scope_authorities[id(prepared)] = prepared

        result = runtime.execute(
            prepared=prepared,
            trigger_id=plan.decision_id,
            started_at=STARTED_AT,
            materialize_exposure=False,
        )

        assert result.run.plan_id == plan.plan_id
        assert result.run.trigger_id == plan.decision_id
        assert ledger.reservation_decision_origin(result.run.run_id) is None
    finally:
        importlib.reload(instance_guard)
        importlib.reload(callsite_guard)
        importlib.reload(resume_guard)


def test_guard_reload_repairs_tampered_delegate_mirrors_from_closure_seal(
    tmp_path,
    monkeypatch,
) -> None:
    installed_reserve = PaperExecutionLedger.reserve_run
    installed_events = PaperExecutionLedger.events
    attacker_calls: list[str] = []

    def forged(*args, **kwargs):
        attacker_calls.append("forged")
        raise AssertionError("tampered delegate mirror must not become authority")

    monkeypatch.setattr(instance_guard, "_STABLE_VERIFIED_SNAPSHOT", forged)
    monkeypatch.setattr(instance_guard, "_STABLE_RESERVE_RUN", forged)
    monkeypatch.setattr(instance_guard, "_STABLE_APPEND_EVENT", forged)
    monkeypatch.setattr(resume_guard, "_STABLE_EVENTS", forged)
    monkeypatch.setattr(resume_guard, "_STABLE_LOAD_RUN", forged)
    monkeypatch.setattr(
        JsonlDecisionLedger,
        instance_guard._VERIFIED_SNAPSHOT_SENTINEL,
        forged,
    )
    monkeypatch.setattr(
        PaperExecutionLedger,
        instance_guard._RESERVE_SENTINEL,
        forged,
    )
    monkeypatch.setattr(
        PaperExecutionLedger,
        instance_guard._APPEND_SENTINEL,
        forged,
    )
    monkeypatch.setattr(
        PaperExecutionLedger,
        resume_guard._EVENTS_SENTINEL,
        forged,
    )
    monkeypatch.setattr(
        PaperExecutionLedger,
        resume_guard._LOAD_SENTINEL,
        forged,
    )

    importlib.reload(instance_guard)
    importlib.reload(resume_guard)

    assert PaperExecutionLedger.reserve_run is installed_reserve
    assert PaperExecutionLedger.events is installed_events
    assert instance_guard._STABLE_RESERVE_RUN is not forged
    assert instance_guard._STABLE_APPEND_EVENT is not forged
    assert instance_guard._STABLE_VERIFIED_SNAPSHOT is not forged
    assert resume_guard._STABLE_EVENTS is not forged
    assert resume_guard._STABLE_LOAD_RUN is not forged

    ledger = PaperExecutionLedger(tmp_path / "sealed-reload.jsonl")
    plan = _plan("decision-sealed-reload")
    ledger.reserve_run(
        run_id="sealed-reload-run",
        trigger_id=plan.decision_id,
        plan=plan,
        config=_config(),
        started_at=STARTED_AT,
        observation_evidence_ids={},
    )
    assert ledger.reservation_decision_origin("sealed-reload-run") is None
    assert [event["event_type"] for event in ledger.events("sealed-reload-run")] == [
        "RUN_RESERVED"
    ]
    assert attacker_calls == []
