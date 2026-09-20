from __future__ import annotations

import pytest

import autosport.evaluation_universe as evaluation_universe_module
from autosport.evaluation_intake import (
    ObservationEnumerationWitness,
    ObservationIntakeLedger,
)
from autosport.evaluation_universe import (
    EvaluationRow,
    EvaluationUniverseIntegrityError,
    EvaluationUniverseLedger,
    EvaluationUniverseStore,
    FunnelEvent,
    FunnelStage,
    SlotState,
    build_frozen_universe,
)
from autosport.monotonic_workspace_authority import MonotonicAuthorityConflictError


H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


class _Resolver:
    def __init__(self, witness: ObservationEnumerationWitness) -> None:
        self.witness = witness

    def resolve_enumeration(self, enumeration_id: str) -> ObservationEnumerationWitness:
        if enumeration_id != self.witness.enumeration_id:
            raise KeyError(enumeration_id)
        return self.witness

    def terminal_enumeration_id(self, **identity: str) -> str:
        del identity
        return self.witness.enumeration_id


def _row() -> EvaluationRow:
    return EvaluationRow(
        row_key="candidate",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H1,
        universe_id="universe-1",
        slot_state=SlotState.CANDIDATE,
        decision_stage=FunnelStage.EXECUTION_MODEL_ELIGIBLE,
        attrition_reason=None,
        sport="football",
        provider_id="provider-1",
        source_id="source-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        source_at="2026-09-20T00:00:00Z",
        received_at="2026-09-20T00:00:01Z",
        committed_at="2026-09-20T00:00:02Z",
        detection_at="2026-09-20T00:00:03Z",
        decision_at="2026-09-20T00:00:04Z",
        quote_set_sha256=H2,
        freshness_policy_sha256=H3,
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        config_sha256=H1,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-1",
        terminal_space_proof_id="terminal-proof-1",
        settlement_proof_id="settlement-rule-proof-1",
        execution_model_id="paper-model-fingerprint-1",
        execution_run_id="paper-run-1",
        execution_plan_id="paper-plan-1",
        execution_action_id="paper-action-1",
        decision_quote_id="quote-1",
        cost_contract_sha256=H2,
        outcome_reveal_not_before="2026-09-20T00:10:00Z",
        dependence_cluster_keys=("event:event-1",),
    )


def _build(tmp_path):
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "authority"
    item = _row()
    witness = ObservationEnumerationWitness(
        enumeration_id="enumeration-1",
        session_id="session-1",
        source_id="source-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H1,
        universe_id="universe-1",
        cycle_index=1,
        source_range_id="range-1",
        stream_epoch="epoch-1",
        start_cursor="cursor-0",
        end_cursor="cursor-1",
        acquisition_sha256=H3,
        row_keys=(item.row_key,),
        row_evidence_sha256=((item.row_key, item.row_id),),
        exhaustive=True,
        gap_free=True,
        committed_at="2026-09-20T00:00:02.500000Z",
        evaluation_not_before="2026-09-20T00:00:03Z",
        outcome_reveal_not_before=item.outcome_reveal_not_before,
    )
    intake = ObservationIntakeLedger(
        workspace,
        authority_id="intake-1",
        enumeration_resolver=_Resolver(witness),
    )
    intake.append_cycle(enumeration_id=witness.enumeration_id)
    frozen = build_frozen_universe(
        intake_ledger=intake,
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H1,
        frozen_at="2026-09-20T00:05:00Z",
        rows=(item,),
    )
    first = EvaluationUniverseLedger(frozen)
    second = first.append(
        FunnelEvent(
            row_id=item.row_id,
            stage=FunnelStage.ATTEMPTED,
            event_at="2026-09-20T00:05:01Z",
            execution_model_id=item.execution_model_id,
            execution_attempt_id="attempt-1",
        )
    )
    return workspace, authority_root, intake, first, second


def test_aborted_prepare_does_not_brick_exact_retry(tmp_path, monkeypatch):
    workspace, authority_root, intake, first, second = _build(tmp_path)
    store = EvaluationUniverseStore(
        workspace,
        intake_ledger=intake,
        authority_root=authority_root,
    )
    store.save(first)

    real_write = evaluation_universe_module.atomic_write_json

    def crash_before_publish(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("simulated crash after PREPARE")

    monkeypatch.setattr(
        evaluation_universe_module,
        "atomic_write_json",
        crash_before_publish,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        store.save(second)

    monkeypatch.setattr(evaluation_universe_module, "atomic_write_json", real_write)
    restarted = EvaluationUniverseStore(
        workspace,
        intake_ledger=intake,
        authority_root=authority_root,
    )
    recovered = restarted.load()
    assert recovered is not None
    assert recovered.ledger_sha256 == first.ledger_sha256

    restarted.save(second)
    final = restarted.load()
    assert final is not None
    assert final.ledger_sha256 == second.ledger_sha256


def test_published_prepare_is_committed_on_restart(tmp_path, monkeypatch):
    workspace, authority_root, intake, first, second = _build(tmp_path)
    store = EvaluationUniverseStore(
        workspace,
        intake_ledger=intake,
        authority_root=authority_root,
    )
    store.save(first)

    def crash_before_commit(**kwargs):
        del kwargs
        raise MonotonicAuthorityConflictError("simulated crash before COMMIT")

    monkeypatch.setattr(store.monotonic_authority, "commit", crash_before_commit)
    with pytest.raises(
        EvaluationUniverseIntegrityError,
        match="monotonic evaluation-universe publication failed closed",
    ):
        store.save(second)

    restarted = EvaluationUniverseStore(
        workspace,
        intake_ledger=intake,
        authority_root=authority_root,
    )
    recovered = restarted.load()
    assert recovered is not None
    assert recovered.ledger_sha256 == second.ledger_sha256
