from __future__ import annotations

from dataclasses import replace

import pytest

import autosport.provider_observation_authority as provider_module
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
from autosport.event_lifecycle import (
    CatalogEvent,
    CatalogPage,
    ContinuousEventLifecycle,
    EventPhase,
)
from autosport.provider_evaluation_universe import (
    ProviderEvaluationUniverseStore,
    build_frozen_universe_from_complete_game_board,
    complete_game_board_member_specs,
)
from autosport.provider_observation_authority import (
    CompleteGameBoardRequest,
    capture_parlay_complete_game_board,
)
from autosport.monotonic_workspace_authority import MonotonicAuthorityConflictError


H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


PROVIDER_CAPTURED_AT = "2026-09-20T00:00:01Z"
PROVIDER_EVALUATION_AT = "2026-09-20T00:00:03Z"
PROVIDER_FROZEN_AT = "2026-09-20T00:05:00Z"
PROVIDER_REVEAL_AT = "2026-09-20T00:10:00Z"


def _provider_request() -> CompleteGameBoardRequest:
    return CompleteGameBoardRequest(
        sport_key="football",
        bookmakers=("bovada",),
        max_age_s=600,
    )


def _provider_frame() -> dict[str, object]:
    return {
        "type": "initial_state",
        "sport_key": "football",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "missing_books": [],
        "truncated_books": [],
        "snapshot_partial_reasons": [],
        "count": 1,
        "timestamp": 1789862401,
        "data": [
            {
                "event_id": "event-1",
                "commence_time_reported": True,
                "commence_time": PROVIDER_REVEAL_AT,
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "h2h",
                "home_ml": -110,
                "away_ml": 105,
                "last_update": "2026-09-20T00:00:00Z",
            }
        ],
    }


def _capture_provider_board():
    patcher = pytest.MonkeyPatch()
    patcher.setattr(
        provider_module,
        "_read_production_initial_state",
        lambda request_scope, *, api_key, timeout_seconds: _provider_frame(),
    )
    patcher.setattr(provider_module, "_default_clock", lambda: PROVIDER_CAPTURED_AT)
    try:
        return capture_parlay_complete_game_board(
            api_key="test-key",
            request=_provider_request(),
            timeout_seconds=1.0,
        )
    finally:
        patcher.undo()


def _provider_lifecycle(workspace) -> ContinuousEventLifecycle:
    workspace.mkdir(parents=True, exist_ok=True)
    request = _provider_request()
    lifecycle = ContinuousEventLifecycle(workspace / "event-lifecycle.json")
    lifecycle.apply_page(
        CatalogPage(
            source_id=request.source_id,
            stream_epoch="epoch-1",
            cursor="cursor-1",
            position=0,
            events=(
                CatalogEvent(
                    source_id=request.source_id,
                    sport=request.sport_key,
                    event_id="event-1",
                    phase=EventPhase.PRE_MATCH,
                    available_at="2026-09-20T00:00:00Z",
                    scheduled_start_at=PROVIDER_REVEAL_AT,
                ),
            ),
        ),
        discovered_at=PROVIDER_CAPTURED_AT,
    )
    return lifecycle


def _providerize_row(template: EvaluationRow, snapshot, member) -> EvaluationRow:
    return replace(
        template,
        row_key=member.row_key,
        sport=snapshot.request.sport_key,
        provider_id="parlayapi",
        source_id=snapshot.request.source_id,
        event_id=member.event_id,
        market_id=member.market_id,
        selection_id=member.selection_id,
        source_at=member.source_at,
        received_at=PROVIDER_CAPTURED_AT,
        committed_at="2026-09-20T00:00:02Z",
        detection_at=PROVIDER_EVALUATION_AT if template.detection_at is not None else None,
        decision_at="2026-09-20T00:00:04Z" if template.decision_at is not None else None,
        execution_run_id=(
            f"{template.execution_run_id}:{member.selection_id}"
            if template.execution_run_id is not None
            else None
        ),
        execution_plan_id=(
            f"{template.execution_plan_id}:{member.selection_id}"
            if template.execution_plan_id is not None
            else None
        ),
        execution_action_id=(
            f"{template.execution_action_id}:{member.selection_id}"
            if template.execution_action_id is not None
            else None
        ),
        decision_quote_id=(
            f"{template.decision_quote_id}:{member.selection_id}"
            if template.decision_quote_id is not None
            else None
        ),
        outcome_reveal_not_before=member.outcome_reveal_not_before,
    )


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
    snapshot = _capture_provider_board()
    lifecycle = _provider_lifecycle(workspace)
    template = _row()
    members = complete_game_board_member_specs(snapshot, event_lifecycle=lifecycle)
    rows = tuple(_providerize_row(template, snapshot, member) for member in members)
    item = rows[0]
    frozen = build_frozen_universe_from_complete_game_board(
        snapshot=snapshot,
        event_lifecycle=lifecycle,
        authority_id="provider-intake-1",
        session_id="session-1",
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H1,
        evaluation_not_before=PROVIDER_EVALUATION_AT,
        frozen_at=PROVIDER_FROZEN_AT,
        rows=rows,
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
    store_kwargs = {
        "authority_id": "provider-intake-1",
        "source_id": snapshot.request.source_id,
        "authority_root": authority_root,
    }
    return workspace, store_kwargs, first, second

def test_aborted_prepare_does_not_brick_exact_retry(tmp_path, monkeypatch):
    workspace, store_kwargs, first, second = _build(tmp_path)
    store = ProviderEvaluationUniverseStore(
        workspace,
        **store_kwargs,
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
    restarted = ProviderEvaluationUniverseStore(
        workspace,
        **store_kwargs,
    )
    recovered = restarted.load()
    assert recovered is not None
    assert recovered.ledger_sha256 == first.ledger_sha256

    restarted.save(second)
    final = restarted.load()
    assert final is not None
    assert final.ledger_sha256 == second.ledger_sha256


def test_published_prepare_is_committed_on_restart(tmp_path, monkeypatch):
    workspace, store_kwargs, first, second = _build(tmp_path)
    store = ProviderEvaluationUniverseStore(
        workspace,
        **store_kwargs,
    )
    store.save(first)

    def crash_before_commit(**kwargs):
        del kwargs
        raise MonotonicAuthorityConflictError("simulated crash before COMMIT")

    monkeypatch.setattr(store._store.monotonic_authority, "commit", crash_before_commit)
    with pytest.raises(
        EvaluationUniverseIntegrityError,
        match="monotonic evaluation-universe publication failed closed",
    ):
        store.save(second)

    restarted = ProviderEvaluationUniverseStore(
        workspace,
        **store_kwargs,
    )
    recovered = restarted.load()
    assert recovered is not None
    assert recovered.ledger_sha256 == second.ledger_sha256
