from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from autosport._provider_evaluation_semantic_gate import (
    _set_legacy_provider_semantic_bypass_for_tests,
)
import autosport.provider_observation_authority as provider_module
from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationRow,
    EvaluationUniverseLedger,
    FunnelStage,
    SlotState,
)
from autosport.event_lifecycle import ContinuousEventLifecycle
from autosport.forward_evaluation_universe_binding import (
    FORWARD_UNIVERSE_RULE_ID,
    FORWARD_UNIVERSE_RULE_SHA256,
    ForwardEvaluationUniverseBindingError,
    authorize_forward_source_receipts,
    resolve_forward_universe_members,
)
from autosport.forward_evidence_completeness import (
    CampaignEvidence,
    CostEvidence,
    DecisionState,
    ForwardEvidenceProtocolEnvelope,
    ForwardOpportunityEnvelope,
    UniverseResult,
    VerificationCode,
    verify_campaign,
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


CAPTURED_AT = "2026-09-20T08:00:00Z"
EVALUATION_NOT_BEFORE = "2026-09-20T08:00:01Z"
FROZEN_AT = "2026-09-20T08:00:02Z"
REVEAL_NOT_BEFORE = "2026-09-20T09:00:00Z"
PROTOCOL_SHA = "1" * 64
QUOTE_SHA = "2" * 64
FRESHNESS_SHA = "3" * 64
CONFIG_SHA = "4" * 64
COST_SHA = "5" * 64
RUNTIME_SHA = "6" * 64


def _request() -> CompleteGameBoardRequest:
    return CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("bovada",),
        max_age_s=600,
    )


def _frame(*, empty: bool) -> dict[str, object]:
    data: list[dict[str, object]] = []
    if not empty:
        data.append(
            {
                "event_id": "event-1",
                "commence_time_reported": True,
                "commence_time": REVEAL_NOT_BEFORE,
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "h2h",
                "home_ml": -110,
                "away_ml": 105,
                "last_update": "2026-09-20T07:59:55Z",
            }
        )
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
        "count": len(data),
        "timestamp": 1789891200,
        "data": data,
    }


def _capture(monkeypatch, *, empty: bool):
    frame = _frame(empty=empty)
    monkeypatch.setattr(
        provider_module,
        "_read_production_initial_state",
        lambda request_scope, *, api_key, timeout_seconds: frame,
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
        outcome_reveal_not_before=member.outcome_reveal_not_before,
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
        outcome_reveal_not_before=None,
        dependence_cluster_keys=("source:table_tennis",),
    )


def _stored_universe(tmp_path, monkeypatch, *, empty: bool) -> ProviderEvaluationUniverseStore:
    snapshot = _capture(monkeypatch, empty=empty)
    lifecycle = None if empty else ContinuousEventLifecycle(tmp_path / "events.json")
    members = complete_game_board_member_specs(snapshot, event_lifecycle=lifecycle)
    rows = tuple(
        _empty_row(snapshot, member) if empty else _candidate_row(snapshot, member)
        for member in members
    )
    # #1185 tests the forward receipt/store binding, not the independent #662
    # product-semantic origin gate.  Use that gate's explicit legacy-fixture
    # compatibility hook only while constructing this provider-universe fixture.
    _set_legacy_provider_semantic_bypass_for_tests(True)
    try:
        universe = build_frozen_universe_from_complete_game_board(
            snapshot=snapshot,
            event_lifecycle=lifecycle,
            authority_id="provider-intake-1",
            session_id="session-1",
            universe_id="universe-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=PROTOCOL_SHA,
            evaluation_not_before=EVALUATION_NOT_BEFORE,
            frozen_at=FROZEN_AT,
            rows=rows,
        )
    finally:
        _set_legacy_provider_semantic_bypass_for_tests(False)
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "authority"
    store = ProviderEvaluationUniverseStore(
        workspace,
        authority_id="provider-intake-1",
        source_id=snapshot.request.source_id,
        authority_root=authority_root,
    )
    store.save(EvaluationUniverseLedger(universe))
    return ProviderEvaluationUniverseStore(
        workspace,
        authority_id="provider-intake-1",
        source_id=snapshot.request.source_id,
        authority_root=authority_root,
    )


def _protocol(**changes) -> ForwardEvidenceProtocolEnvelope:
    values = {
        "campaign_id": "campaign-1",
        "scientific_protocol_sha256": PROTOCOL_SHA,
        "candidate_universe_rule_id": FORWARD_UNIVERSE_RULE_ID,
        "candidate_universe_rule_sha256": FORWARD_UNIVERSE_RULE_SHA256,
        "forward_evaluation_policy_sha256": "7" * 64,
        "runtime_identity_sha256": RUNTIME_SHA,
        "baseline_set_sha256": "8" * 64,
        "protective_metric_set_sha256": "9" * 64,
        "cost_policy_sha256": "a" * 64,
        "precommit_anchor_lower": datetime(2026, 9, 20, 7, 0, tzinfo=UTC),
        "precommit_anchor_upper": datetime(2026, 9, 20, 7, 30, tzinfo=UTC),
    }
    values.update(changes)
    return ForwardEvidenceProtocolEnvelope(**values)


def _opportunities(protocol, expectations):
    items = []
    predecessor = "GENESIS"
    for sequence, expected in enumerate(expectations, 1):
        item = ForwardOpportunityEnvelope(
            campaign_id=protocol.campaign_id,
            protocol_sha256=protocol.protocol_sha256,
            candidate_sequence=sequence,
            opportunity_id=expected.opportunity_id,
            source_receipt_id=expected.source_receipt_id,
            source_receipt_sha256=expected.source_receipt_sha256,
            causal_cutoff=expected.causal_cutoff,
            observed_lower=expected.observed_lower,
            observed_upper=expected.observed_upper,
            universe_rule_result=expected.universe_rule_result,
            universe_rule_reason_code=expected.universe_rule_reason_code,
            provider_acquisition_state=expected.provider_acquisition_state,
            reveal_boundary_receipt_id=f"boundary-{sequence}",
            runtime_identity_sha256=protocol.runtime_identity_sha256,
            predecessor_opportunity_sha256=predecessor,
            decision_state=DecisionState.NO_BET,
        )
        items.append(item)
        predecessor = item.opportunity_sha256
    return tuple(items)


def test_empty_complete_board_becomes_authoritative_excluded_receipt_after_restart(
    tmp_path, monkeypatch
):
    store = _stored_universe(tmp_path, monkeypatch, empty=True)
    protocol = _protocol()

    expectations = resolve_forward_universe_members(store=store, protocol=protocol)

    assert len(expectations) == 1
    assert expectations[0].universe_rule_result is UniverseResult.EXCLUDED
    assert expectations[0].universe_rule_reason_code == "NO_EVENT"
    assert expectations[0].provider_acquisition_state == "NO_EVENT"
    opportunities = _opportunities(protocol, expectations)
    receipts = authorize_forward_source_receipts(
        store=store,
        protocol=protocol,
        opportunities=opportunities,
    )
    assert len(receipts) == 1
    assert receipts[0].receipt_id == opportunities[0].source_receipt_id
    assert receipts[0].receipt_sha256 == opportunities[0].source_receipt_sha256
    assert receipts[0].opportunity_id == opportunities[0].opportunity_id
    assert receipts[0].universe_rule_result is UniverseResult.EXCLUDED


def test_provider_present_members_are_admitted_from_exact_durable_universe(
    tmp_path, monkeypatch
):
    store = _stored_universe(tmp_path, monkeypatch, empty=False)
    protocol = _protocol()

    expectations = resolve_forward_universe_members(store=store, protocol=protocol)

    assert len(expectations) == 2
    assert {item.universe_rule_result for item in expectations} == {UniverseResult.ADMITTED}
    assert {item.universe_rule_reason_code for item in expectations} == {"CANDIDATE"}
    assert {item.provider_acquisition_state for item in expectations} == {"CANDIDATE"}
    receipts = authorize_forward_source_receipts(
        store=store,
        protocol=protocol,
        opportunities=_opportunities(protocol, expectations),
    )
    assert len(receipts) == 2


def test_omission_and_invention_fail_closed(tmp_path, monkeypatch):
    store = _stored_universe(tmp_path, monkeypatch, empty=False)
    protocol = _protocol()
    expectations = resolve_forward_universe_members(store=store, protocol=protocol)
    opportunities = _opportunities(protocol, expectations)

    with pytest.raises(ForwardEvaluationUniverseBindingError, match="does not equal durable"):
        authorize_forward_source_receipts(
            store=store,
            protocol=protocol,
            opportunities=opportunities[:-1],
        )

    invented = replace(
        opportunities[-1],
        source_receipt_id="invented-receipt",
        source_receipt_sha256="b" * 64,
    )
    with pytest.raises(ForwardEvaluationUniverseBindingError, match="does not equal durable"):
        authorize_forward_source_receipts(
            store=store,
            protocol=protocol,
            opportunities=(*opportunities[:-1], invented),
        )


def test_relabelled_result_or_acquisition_state_fails_closed(tmp_path, monkeypatch):
    store = _stored_universe(tmp_path, monkeypatch, empty=True)
    protocol = _protocol()
    expectations = resolve_forward_universe_members(store=store, protocol=protocol)
    opportunity = _opportunities(protocol, expectations)[0]

    with pytest.raises(ForwardEvaluationUniverseBindingError, match="does not exactly match"):
        authorize_forward_source_receipts(
            store=store,
            protocol=protocol,
            opportunities=(replace(opportunity, universe_rule_result=UniverseResult.ADMITTED),),
        )
    with pytest.raises(ForwardEvaluationUniverseBindingError, match="does not exactly match"):
        authorize_forward_source_receipts(
            store=store,
            protocol=protocol,
            opportunities=(replace(opportunity, provider_acquisition_state="CANDIDATE"),),
        )


def test_forward_protocol_must_precommit_before_durable_source_observation(
    tmp_path, monkeypatch
):
    store = _stored_universe(tmp_path, monkeypatch, empty=True)
    protocol = _protocol(
        precommit_anchor_lower=datetime(2026, 9, 20, 8, 0, tzinfo=UTC),
        precommit_anchor_upper=datetime(2026, 9, 20, 8, 0, tzinfo=UTC),
    )

    with pytest.raises(ForwardEvaluationUniverseBindingError, match="anchored before"):
        resolve_forward_universe_members(store=store, protocol=protocol)


def test_receipt_digest_is_bound_to_forward_protocol(tmp_path, monkeypatch):
    store = _stored_universe(tmp_path, monkeypatch, empty=True)
    first = _protocol(forward_evaluation_policy_sha256="c" * 64)
    second = _protocol(forward_evaluation_policy_sha256="d" * 64)

    first_receipt = resolve_forward_universe_members(store=store, protocol=first)[0]
    second_receipt = resolve_forward_universe_members(store=store, protocol=second)[0]

    assert first_receipt.source_receipt_id == second_receipt.source_receipt_id
    assert first_receipt.source_receipt_sha256 != second_receipt.source_receipt_sha256


def test_product_resolved_receipts_satisfy_parent_bidirectional_source_inventory(
    tmp_path, monkeypatch
):
    store = _stored_universe(tmp_path, monkeypatch, empty=True)
    protocol = _protocol()
    expectations = resolve_forward_universe_members(store=store, protocol=protocol)
    opportunities = _opportunities(protocol, expectations)
    receipts = authorize_forward_source_receipts(
        store=store,
        protocol=protocol,
        opportunities=opportunities,
    )

    result = verify_campaign(
        CampaignEvidence(
            protocol=protocol,
            opportunities=opportunities,
            cohort_roots=(),
            closes=(),
            reveal_boundaries=(),
            authoritative_receipts=receipts,
            denominator_sequences=(1,),
            cost_evidence=(CostEvidence(1, True),),
        )
    )

    assert VerificationCode.COHORT_OMISSION_DETECTED not in result.codes
    assert result.ok is False


def test_wrong_candidate_rule_or_fake_store_cannot_mint_authority(tmp_path, monkeypatch):
    store = _stored_universe(tmp_path, monkeypatch, empty=True)
    wrong_rule = _protocol(candidate_universe_rule_sha256="e" * 64)
    with pytest.raises(ForwardEvaluationUniverseBindingError, match="does not precommit"):
        resolve_forward_universe_members(store=store, protocol=wrong_rule)

    class FakeStore:
        def load(self):
            return store.load()

    with pytest.raises(TypeError, match="exact ProviderEvaluationUniverseStore"):
        resolve_forward_universe_members(store=FakeStore(), protocol=_protocol())

def test_exact_store_instance_load_rebinding_cannot_replace_durable_authority(
    tmp_path, monkeypatch
):
    store = _stored_universe(tmp_path, monkeypatch, empty=False)
    protocol = _protocol()
    expectations = resolve_forward_universe_members(store=store, protocol=protocol)
    opportunities = _opportunities(protocol, expectations)
    trusted_ledger = ProviderEvaluationUniverseStore.load(store)
    assert trusted_ledger is not None

    empty_store = ProviderEvaluationUniverseStore(
        tmp_path / "empty-workspace",
        authority_id=store.authority_id,
        source_id=store.source_id,
        authority_root=tmp_path / "empty-authority",
    )
    store._store = empty_store._store
    assert ProviderEvaluationUniverseStore.load(store) is None

    store.load = lambda: trusted_ledger
    assert type(store) is ProviderEvaluationUniverseStore

    with pytest.raises(ForwardEvaluationUniverseBindingError):
        authorize_forward_source_receipts(
            store=store,
            protocol=protocol,
            opportunities=opportunities,
        )

