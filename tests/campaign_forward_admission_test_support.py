from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import autosport.campaign_provider_cycle_capture as capture_module
import autosport.provider_observation_authority as provider_module
from autosport.campaign_inception import (
    CampaignInceptionSourceSpec,
    campaign_evaluation_plan_sha256,
)
from autosport.campaign_precommit_manifest import (
    CampaignPrecommitManifest,
    publish_campaign_precommit_manifest,
)
from autosport.causal_collector import CollectorDeltaStore
from autosport.campaign_forward_universe_cycle_binding import (
    CampaignForwardEvidenceVerification,
    verify_campaign_forward_evidence,
)
from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationRow,
    EvaluationUniverseLedger,
    FunnelStage,
    SlotState,
)
from autosport.forward_evaluation_universe_binding import (
    FORWARD_UNIVERSE_RULE_ID,
    FORWARD_UNIVERSE_RULE_SHA256,
    resolve_forward_universe_members,
)
from autosport.forward_evidence_completeness import (
    AuthoritativeSourceReceipt,
    CampaignCloseEnvelope,
    CampaignEvidence,
    CostEvidence,
    DecisionState,
    ForwardEvidenceProtocolEnvelope,
    ForwardOpportunityEnvelope,
    RevealBoundaryReceipt,
    UniverseResult,
    build_cohort_root,
)
from autosport.forward_universe_precommit_authority import (
    ForwardUniversePrecommitLocator,
)
from autosport.provider_evaluation_universe import (
    ProviderEvaluationUniverseStore,
    build_frozen_universe_from_complete_game_board,
    complete_game_board_member_specs,
)
from autosport.provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardRequest,
)


A = "a" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
ZERO = "0" * 64
ONE = "1" * 64
CAPTURED_AT = "2100-01-01T06:00:00.500000Z"
PROTOCOL_SHA = "9" * 64
FRESHNESS_SHA = "8" * 64


class _FakeSseResponse:
    def __init__(self, frame: dict[str, object]) -> None:
        self.status = 200
        self.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
        encoded = json.dumps(
            frame,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self._lines = [
            b"event: initial_state\n",
            b"data: " + encoded + b"\n",
            b"\n",
        ]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def __iter__(self):
        return iter(self._lines)


def _empty_frame() -> dict[str, object]:
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
        "count": 0,
        "timestamp": 4102466400,
        "data": [],
    }


def _request() -> CompleteGameBoardRequest:
    return CompleteGameBoardRequest(
        sport_key="table_tennis",
        bookmakers=("tenbet", "bovada"),
        max_age_s=600,
    )


def _clock():
    values = iter(
        [
            "2100-01-01T06:00:00+00:00",
            "2100-01-01T06:00:01+00:00",
            "2100-01-01T06:00:02+00:00",
        ]
    )
    return lambda: next(values)


def build_forward_admission_verification(base: Path) -> dict[str, object]:
    """Build one real cycle-bound #2050 PASS receipt plus exact re-resolution inputs."""

    base.mkdir(parents=True, exist_ok=True)
    store_path = base / "collector.db"
    collector_store = CollectorDeltaStore(store_path)
    source_spec = CampaignInceptionSourceSpec(
        expected_store_path=store_path,
        source_id="parlayapi:table_tennis",
        run_id="run-1",
        stream_epoch="complete-board-epoch-1",
        anchor_at="2100-01-01T06:00:00+00:00",
        interval_seconds=10,
        max_items=250,
        evaluation_start_slot_ordinal=0,
        evaluation_end_slot_ordinal=1,
    )
    workspace = base / "workspace"
    evidence_dir = workspace / "evidence"
    evidence_dir.mkdir(parents=True)
    manifest_path = evidence_dir / "precommit.json"
    authority_root = base / "machine-authority"
    evaluation_plan_sha256 = campaign_evaluation_plan_sha256(source_spec)
    publish_campaign_precommit_manifest(
        manifest_path,
        CampaignPrecommitManifest(
            campaign_id="campaign-cycle-capture-test",
            source_id=source_spec.source_id,
            source_snapshot_sha256=A,
            committed_at="2099-12-31T19:00:00Z",
            observation_not_before="2100-01-01T06:00:00Z",
            observation_not_after="2100-01-08T06:00:00Z",
            evaluation_universe_sha256=evaluation_plan_sha256,
            strategy_version_id="strategy-v17",
            champion_version_id="model-v42",
            baseline_version_id="market-baseline-v3",
            cost_contract_sha256=C,
            multiplicity_policy_sha256=D,
            stopping_policy_sha256=E,
            restart_policy_sha256=F,
            causal_evidence_policy_sha256=ZERO,
            config_sha256=ONE,
        ),
        workspace=workspace,
        authority_root=authority_root,
    )
    precommit_locator = ForwardUniversePrecommitLocator(
        manifest_path=manifest_path,
        workspace=workspace,
        authority_root=authority_root,
    )
    provider_evidence_store = CompleteGameBoardEvidenceStore(
        workspace,
        authority_root=authority_root,
    )

    with provider_module._test_acquisition_origin(
        _capability=provider_module._TEST_ACQUISITION_CAPABILITY,
    ):
        with capture_module._test_campaign_clock_origin(
            _capability=capture_module._TEST_CAMPAIGN_CLOCK_CAPABILITY,
        ):
            with patch.object(
                provider_module,
                "urlopen",
                lambda _request, _timeout: _FakeSseResponse(_empty_frame()),
            ), patch.object(
                provider_module,
                "_default_clock",
                lambda: CAPTURED_AT,
            ):
                snapshot, cycle_receipt = (
                    capture_module.capture_campaign_complete_game_board(
                        precommit_locator=precommit_locator,
                        store=collector_store,
                        source_spec=source_spec,
                        evidence_store=provider_evidence_store,
                        request=_request(),
                        api_key="test-only-secret",
                        timeout_seconds=3.0,
                        clock=_clock(),
                    )
                )

    members = complete_game_board_member_specs(snapshot, event_lifecycle=None)
    if len(members) != 1:
        raise AssertionError("forward admission fixture expected one NO_EVENT member")
    member = members[0]
    row = EvaluationRow(
        row_key=member.row_key,
        campaign_id="campaign-cycle-capture-test",
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
        strategy_version_id="strategy-v17",
        model_version_id=None,
        config_sha256=ONE,
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
        cost_contract_sha256=C,
        outcome_reveal_not_before=None,
        dependence_cluster_keys=("source:table_tennis",),
    )
    universe = build_frozen_universe_from_complete_game_board(
        snapshot=snapshot,
        event_lifecycle=None,
        authority_id="provider-universe-1",
        session_id="session-1",
        universe_id="universe-1",
        campaign_id="campaign-cycle-capture-test",
        research_protocol_id="protocol-1",
        protocol_sha256=PROTOCOL_SHA,
        evaluation_not_before="2100-01-01T06:00:02Z",
        frozen_at="2100-01-01T06:00:03Z",
        rows=(row,),
    )
    universe_store = ProviderEvaluationUniverseStore(
        base / "provider-universe-workspace",
        authority_id="provider-universe-1",
        source_id=source_spec.source_id,
        authority_root=base / "provider-universe-authority",
    )
    universe_store.save(EvaluationUniverseLedger(universe))

    protocol = ForwardEvidenceProtocolEnvelope(
        campaign_id="campaign-cycle-capture-test",
        scientific_protocol_sha256=PROTOCOL_SHA,
        candidate_universe_rule_id=FORWARD_UNIVERSE_RULE_ID,
        candidate_universe_rule_sha256=FORWARD_UNIVERSE_RULE_SHA256,
        forward_evaluation_policy_sha256="7" * 64,
        runtime_identity_sha256="6" * 64,
        baseline_set_sha256="5" * 64,
        protective_metric_set_sha256="4" * 64,
        cost_policy_sha256="3" * 64,
        precommit_anchor_lower=datetime(2099, 12, 31, 19, 0, tzinfo=timezone.utc),
        precommit_anchor_upper=datetime(2099, 12, 31, 19, 30, tzinfo=timezone.utc),
    )
    expectations = resolve_forward_universe_members(
        store=universe_store,
        protocol=protocol,
        precommit=precommit_locator,
    )
    if len(expectations) != 1:
        raise AssertionError("forward admission fixture expected one source receipt")
    expected = expectations[0]
    opportunity = ForwardOpportunityEnvelope(
        campaign_id=protocol.campaign_id,
        protocol_sha256=protocol.protocol_sha256,
        candidate_sequence=1,
        opportunity_id=expected.opportunity_id,
        source_receipt_id=expected.source_receipt_id,
        source_receipt_sha256=expected.source_receipt_sha256,
        causal_cutoff=expected.causal_cutoff,
        observed_lower=expected.observed_lower,
        observed_upper=expected.observed_upper,
        universe_rule_result=expected.universe_rule_result,
        universe_rule_reason_code=expected.universe_rule_reason_code,
        provider_acquisition_state=expected.provider_acquisition_state,
        reveal_boundary_receipt_id="boundary-1",
        runtime_identity_sha256=protocol.runtime_identity_sha256,
        predecessor_opportunity_sha256="GENESIS",
        decision_state=DecisionState.NO_BET,
    )
    root = build_cohort_root(
        (opportunity,),
        anchor_lower=datetime(2100, 1, 1, 6, 0, 2, tzinfo=timezone.utc),
        anchor_upper=datetime(2100, 1, 1, 6, 0, 2, 100000, tzinfo=timezone.utc),
    )
    boundary = RevealBoundaryReceipt(
        receipt_id="boundary-1",
        campaign_id=protocol.campaign_id,
        event_or_market_id="empty-board-window",
        provider_or_authority_id="provider-universe-1",
        boundary_rule_id="test-empty-board-boundary-v1",
        source_receipt_id=expected.source_receipt_id,
        source_sha256=expected.source_receipt_sha256,
        boundary_lower=datetime(2100, 1, 1, 6, 0, 10, tzinfo=timezone.utc),
        boundary_upper=datetime(2100, 1, 1, 6, 0, 11, tzinfo=timezone.utc),
        uncertainty_basis="deterministic integration fixture",
    )
    close = CampaignCloseEnvelope(
        campaign_id=protocol.campaign_id,
        protocol_sha256=protocol.protocol_sha256,
        terminal_cohort_root_sha256=root.cohort_root_sha256,
        final_candidate_count=1,
        first_sequence=1,
        last_sequence=1,
        close_reason="integration fixture complete",
        anchor_lower=datetime(2100, 1, 1, 6, 0, 3, tzinfo=timezone.utc),
        anchor_upper=datetime(2100, 1, 1, 6, 0, 3, 100000, tzinfo=timezone.utc),
    )
    forged = AuthoritativeSourceReceipt(
        receipt_id="forged-receipt",
        receipt_sha256="f" * 64,
        campaign_id=protocol.campaign_id,
        opportunity_id="forged-opportunity",
        universe_rule_result=UniverseResult.EXCLUDED,
    )
    campaign_evidence = CampaignEvidence(
        protocol=protocol,
        opportunities=(opportunity,),
        cohort_roots=(root,),
        closes=(close,),
        reveal_boundaries=(boundary,),
        authoritative_receipts=(forged,),
        denominator_sequences=(1,),
        cost_evidence=(CostEvidence(1, True),),
    )

    verification = verify_campaign_forward_evidence(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        event_lifecycle=None,
        evidence=campaign_evidence,
    )
    if type(verification) is not CampaignForwardEvidenceVerification:
        raise AssertionError("forward admission fixture returned noncanonical verification")
    if (
        verification.structural_ok is not True
        or verification.structural_codes != ("PASS",)
        or verification.verification_scope
        != "CYCLE_BOUND_PROVIDER_UNIVERSE_STRUCTURAL_ONLY"
        or verification.provider_universe_authority_resolved is not True
        or verification.promotion_ready is not False
        or verification.real_money_ready is not False
        or verification.prospective_evaluation_plan_sha256 != evaluation_plan_sha256
        or verification.universe_sha256 != universe.universe_sha256
        or verification.membership_sha256 != universe.membership_sha256
    ):
        raise AssertionError("forward admission fixture verification truth changed")

    return {
        "campaign_forward_verification": verification,
        "campaign_forward_precommit_locator": precommit_locator,
        "campaign_forward_collector_store": collector_store,
        "campaign_forward_source_spec": source_spec,
        "campaign_forward_cycle_receipt": cycle_receipt,
        "campaign_forward_provider_evidence_store": provider_evidence_store,
        "campaign_forward_universe_store": universe_store,
        "campaign_forward_event_lifecycle": None,
        "campaign_forward_evidence": campaign_evidence,
    }
