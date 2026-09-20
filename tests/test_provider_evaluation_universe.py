from __future__ import annotations

import json
from dataclasses import replace

import pytest

import autosport.provider_observation_authority as provider_module
from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationRow,
    EvaluationUniverse,
    EvaluationUniverseLedger,
    FunnelStage,
    SlotState,
)
from autosport.provider_evaluation_universe import (
    ProviderEvaluationUniverseError,
    ProviderEvaluationUniverseStore,
    build_frozen_universe_from_complete_game_board,
    complete_game_board_member_specs,
)
from autosport.provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    capture_parlay_complete_game_board,
)


CAPTURED_AT = "2026-09-20T08:00:00Z"
EVALUATION_NOT_BEFORE = "2026-09-20T08:00:01Z"
FROZEN_AT = "2026-09-20T08:00:02Z"
REVEAL_NOT_BEFORE = "2026-09-20T09:00:00Z"
PROTOCOL_SHA = "1" * 64
QUOTE_SHA = "2" * 64
FRESHNESS_SHA = "3" * 64
CONFIG_SHA = "4" * 64
COST_SHA = "5" * 64


def _request() -> CompleteGameBoardRequest:
    return CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("bovada", "tenbet"),
        max_age_s=600,
    )


def _frame() -> dict[str, object]:
    return {
        "type": "initial_state",
        "sport_key": "table_tennis",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "missing_books": [],
        "truncated_books": [],
        "snapshot_partial_reasons": [],
        "count": 3,
        "timestamp": 1789891200,
        "data": [
            {
                "event_id": "event-1",
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "h2h",
                "home_ml": -110,
                "away_ml": 105,
                "last_update": "2026-09-20T07:59:55Z",
            },
            {
                "event_id": "event-1",
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "spreads",
                "line": -1.5,
                "home_price": 120,
                "away_price": -125,
                "last_update": "2026-09-20T07:59:55Z",
            },
            {
                "event_id": "event-1",
                "bookmaker": "tenbet",
                "kind": "game",
                "market_key": "totals",
                "line": 74.5,
                "over_price": -105,
                "under_price": -110,
                "last_update": "2026-09-20T07:59:54Z",
            },
        ],
    }


def _capture(monkeypatch, frame: dict[str, object] | None = None):
    provider_frame = _frame() if frame is None else frame
    monkeypatch.setattr(
        provider_module,
        "_read_production_initial_state",
        lambda request_scope, *, api_key, timeout_seconds: provider_frame,
    )
    monkeypatch.setattr(provider_module, "_default_clock", lambda: CAPTURED_AT)
    return capture_parlay_complete_game_board(
        api_key="test-key",
        request=_request(),
        timeout_seconds=1.0,
    )


def _candidate_row(snapshot, member) -> EvaluationRow:
    return EvaluationRow(
        row_key=member.row_key,
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=PROTOCOL_SHA,
        universe_id="universe-1",
        slot_state=SlotState.CANDIDATE,
        decision_stage=FunnelStage.DETECTED,
        attrition_reason=AttritionReason.THEORETICAL_ONLY,
        sport=snapshot.request.sport_key,
        provider_id="parlayapi",
        source_id=snapshot.request.source_id,
        event_id=member.event_id,
        market_id=member.market_id,
        selection_id=member.selection_id,
        source_at=member.source_at,
        received_at=CAPTURED_AT,
        committed_at=CAPTURED_AT,
        detection_at=EVALUATION_NOT_BEFORE,
        decision_at=None,
        quote_set_sha256=QUOTE_SHA,
        freshness_policy_sha256=FRESHNESS_SHA,
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        config_sha256=CONFIG_SHA,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-1",
        terminal_space_proof_id=None,
        settlement_proof_id=None,
        execution_model_id=None,
        execution_run_id=None,
        execution_plan_id=None,
        execution_action_id=None,
        decision_quote_id=None,
        cost_contract_sha256=COST_SHA,
        outcome_reveal_not_before=REVEAL_NOT_BEFORE,
        dependence_cluster_keys=("event:event-1",),
    )


def _empty_row(snapshot, member) -> EvaluationRow:
    return EvaluationRow(
        row_key=member.row_key,
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=PROTOCOL_SHA,
        universe_id="universe-1",
        slot_state=SlotState.NO_EVENT,
        decision_stage=FunnelStage.OBSERVED_SLOT,
        attrition_reason=AttritionReason.NO_EVENT,
        sport=snapshot.request.sport_key,
        provider_id="parlayapi",
        source_id=snapshot.request.source_id,
        event_id=None,
        market_id=None,
        selection_id=None,
        source_at=member.source_at,
        received_at=CAPTURED_AT,
        committed_at=CAPTURED_AT,
        detection_at=None,
        decision_at=None,
        quote_set_sha256=None,
        freshness_policy_sha256=FRESHNESS_SHA,
        strategy_version_id="strategy-1",
        model_version_id=None,
        config_sha256=CONFIG_SHA,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-1",
        terminal_space_proof_id=None,
        settlement_proof_id=None,
        execution_model_id=None,
        execution_run_id=None,
        execution_plan_id=None,
        execution_action_id=None,
        decision_quote_id=None,
        cost_contract_sha256=COST_SHA,
        outcome_reveal_not_before=REVEAL_NOT_BEFORE,
        dependence_cluster_keys=("source:table_tennis",),
    )


def _build(snapshot, rows):
    return build_frozen_universe_from_complete_game_board(
        snapshot=snapshot,
        authority_id="provider-intake-1",
        session_id="session-1",
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=PROTOCOL_SHA,
        evaluation_not_before=EVALUATION_NOT_BEFORE,
        outcome_reveal_not_before=REVEAL_NOT_BEFORE,
        frozen_at=FROZEN_AT,
        rows=rows,
    )


def test_live_complete_board_freezes_every_selection_and_resumes_after_restart(
    tmp_path, monkeypatch
):
    snapshot = _capture(monkeypatch)
    members = complete_game_board_member_specs(snapshot)
    assert len(members) == 6
    universe = _build(snapshot, tuple(_candidate_row(snapshot, member) for member in members))
    assert universe.intake_snapshot.source_id == "parlayapi:table_tennis"
    assert universe.intake_snapshot.expected_row_keys == tuple(
        sorted(member.row_key for member in members)
    )

    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "authority"
    store = ProviderEvaluationUniverseStore(
        workspace,
        authority_id="provider-intake-1",
        source_id="parlayapi:table_tennis",
        authority_root=authority_root,
    )
    store.save(EvaluationUniverseLedger(universe))

    restarted = ProviderEvaluationUniverseStore(
        workspace,
        authority_id="provider-intake-1",
        source_id="parlayapi:table_tennis",
        authority_root=authority_root,
    )
    loaded = restarted.load()
    assert loaded is not None
    assert loaded.universe.universe_sha256 == universe.universe_sha256
    assert loaded.universe.intake_snapshot.root_sha256 == universe.intake_snapshot.root_sha256


def test_caller_constructed_value_equal_provider_snapshot_cannot_mint_denominator():
    lookalike = CompleteGameBoardSnapshot(
        request=_request(),
        captured_at=CAPTURED_AT,
        frame_json=json.dumps(_frame()),
    )
    with pytest.raises(Exception, match="not issued by canonical provider acquisition evidence"):
        complete_game_board_member_specs(lookalike)


def test_complete_board_rejects_selection_cherry_pick(monkeypatch):
    snapshot = _capture(monkeypatch)
    members = complete_game_board_member_specs(snapshot)
    rows = tuple(_candidate_row(snapshot, member) for member in members[:-1])
    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="must equal every canonical complete-board selection member",
    ):
        _build(snapshot, rows)


def test_complete_board_rejects_member_identity_relabel(monkeypatch):
    snapshot = _capture(monkeypatch)
    members = complete_game_board_member_specs(snapshot)
    rows = [_candidate_row(snapshot, member) for member in members]
    rows[0] = replace(rows[0], selection_id="forged-selection")
    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="does not match exact provider selection membership",
    ):
        _build(snapshot, tuple(rows))


def test_deserialized_consumer_state_cannot_be_first_durable_commit(tmp_path, monkeypatch):
    snapshot = _capture(monkeypatch)
    members = complete_game_board_member_specs(snapshot)
    universe = _build(snapshot, tuple(_candidate_row(snapshot, member) for member in members))
    deserialized = EvaluationUniverse.from_payload(universe.to_payload())

    store = ProviderEvaluationUniverseStore(
        tmp_path / "fresh-workspace",
        authority_id="provider-intake-1",
        source_id="parlayapi:table_tennis",
        authority_root=tmp_path / "fresh-authority",
    )
    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="must come from a live canonical complete-board derivation",
    ):
        store.save(EvaluationUniverseLedger(deserialized))


def test_provider_save_without_consumer_commit_does_not_create_restart_denominator(
    tmp_path, monkeypatch
):
    snapshot = _capture(monkeypatch)
    provider_store = CompleteGameBoardEvidenceStore(
        tmp_path / "workspace",
        authority_root=tmp_path / "provider-authority",
    )
    provider_store.save(snapshot)

    consumer_store = ProviderEvaluationUniverseStore(
        tmp_path / "workspace",
        authority_id="provider-intake-1",
        source_id="parlayapi:table_tennis",
        authority_root=tmp_path / "consumer-authority",
    )
    assert consumer_store.load() is None


def test_empty_complete_board_requires_explicit_no_event_member(tmp_path, monkeypatch):
    frame = _frame()
    frame["data"] = []
    frame["count"] = 0
    snapshot = _capture(monkeypatch, frame)
    members = complete_game_board_member_specs(snapshot)
    assert len(members) == 1
    assert members[0].event_id is None

    universe = _build(snapshot, (_empty_row(snapshot, members[0]),))
    store = ProviderEvaluationUniverseStore(
        tmp_path / "workspace",
        authority_id="provider-intake-1",
        source_id="parlayapi:table_tennis",
        authority_root=tmp_path / "authority",
    )
    store.save(EvaluationUniverseLedger(universe))
    assert store.load() is not None
