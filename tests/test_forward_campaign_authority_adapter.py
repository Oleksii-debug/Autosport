from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.forward_campaign_authority_adapter import (
    ForwardCampaignAuthorityAdapterError,
    admit_negative_sample,
    admit_settled_action_sample,
    admit_settled_evaluation_sample,
)
from autosport.forward_campaign_population import (
    PopulationRole,
    TerminalEvidenceState,
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
    verify_campaign,
)
from autosport.learning_environment import (
    Action,
    EnvironmentCheckpoint,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)
from autosport.paper_abstention_learning import PaperAbstentionFinalizationReceipt
from autosport.paper_settlement_learning import PaperSettlementLearningWitness
from autosport.point_in_time_evidence import FeatureAvailabilityEvidence
from autosport.source_rights_manifest import (
    authorize_source_use,
    load_source_rights_manifest,
)


BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
ENVIRONMENT = "e" * 64
EPISODE = "d" * 64
SOURCE = "licensed-provider-account:fixture"
SCOPE = "provider.market_data.read"


def _iso(seconds: int) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _rights(tmp_path, *, checked_second: int = 0):
    path = tmp_path / f"source-rights-{checked_second}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "autosport_source_rights_manifest",
                "source_identity": SOURCE,
                "authorized_scopes": [SCOPE],
                "privacy_classification": "NON_PERSONAL_DATA",
                "evidence_class": "HUMAN_APPROVED_SOURCE_RIGHTS",
                "terms_reference": "terms:provider:v1",
                "retention_authority_reference": "retention:provider:v1",
                "effective_at": "2026-09-01T00:00:00Z",
                "expires_at": "2026-12-01T00:00:00Z",
                "human_approved": True,
                "approved_by": "release-owner",
                "approval_reference": "entitlement-record:fixture-001",
                "approved_at": "2026-08-31T12:00:00Z",
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest = load_source_rights_manifest(path)
    return authorize_source_use(
        manifest,
        source_identity=SOURCE,
        required_scope=SCOPE,
        at=BASE + timedelta(seconds=checked_second),
    )


def _feature(*, available_second: int = 13, cutoff_second: int = 15):
    return FeatureAvailabilityEvidence(
        feature_identity="table-tennis-forward-features",
        feature_version="1",
        feature_definition_sha256="1" * 64,
        source_identity=SOURCE,
        source_revision="2" * 64,
        revision_policy_id="published-at-or-before-decision-v1",
        dataset_snapshot_id="dataset-1",
        dataset_record_sha256="3" * 64,
        dataset_manifest_sha256="4" * 64,
        dataset_lineage_proof_sha256="5" * 64,
        feature_provenance_sha256="6" * 64,
        feature_payload_sha256="7" * 64,
        as_of_utc=_iso(9),
        available_at_utc=_iso(available_second),
        decision_cutoff_utc=_iso(cutoff_second),
    )


def _campaign(
    state: DecisionState = DecisionState.ACTION,
    *,
    decision_id: str | None = None,
) -> CampaignEvidence:
    protocol = ForwardEvidenceProtocolEnvelope(
        campaign_id="account-c-real-authority-campaign",
        scientific_protocol_sha256="a" * 64,
        candidate_universe_rule_id="all-table-tennis-prematch-v1",
        candidate_universe_rule_sha256="b" * 64,
        forward_evaluation_policy_sha256="c" * 64,
        runtime_identity_sha256="d" * 64,
        baseline_set_sha256="e" * 64,
        protective_metric_set_sha256="f" * 64,
        cost_policy_sha256="0" * 64,
        precommit_anchor_lower=BASE,
        precommit_anchor_upper=BASE + timedelta(seconds=1),
    )
    opportunity = ForwardOpportunityEnvelope(
        campaign_id=protocol.campaign_id,
        protocol_sha256=protocol.protocol_sha256,
        candidate_sequence=1,
        opportunity_id="opportunity-1",
        source_receipt_id="source-receipt-1",
        source_receipt_sha256="8" * 64,
        causal_cutoff=BASE + timedelta(seconds=10),
        observed_lower=BASE + timedelta(seconds=10),
        observed_upper=BASE + timedelta(seconds=11),
        universe_rule_result=UniverseResult.ADMITTED,
        universe_rule_reason_code="MATCH",
        provider_acquisition_state="OK",
        reveal_boundary_receipt_id="reveal-boundary-1",
        runtime_identity_sha256=protocol.runtime_identity_sha256,
        predecessor_opportunity_sha256="GENESIS",
        decision_state=state,
        decision_id=(decision_id or "decision-1") if state is DecisionState.ACTION else None,
        quote_or_market_identity="market-1",
    )
    root = build_cohort_root(
        (opportunity,),
        previous_cohort_root_sha256="GENESIS",
        anchor_lower=BASE + timedelta(seconds=30),
        anchor_upper=BASE + timedelta(seconds=31),
    )
    close = CampaignCloseEnvelope(
        campaign_id=protocol.campaign_id,
        protocol_sha256=protocol.protocol_sha256,
        terminal_cohort_root_sha256=root.cohort_root_sha256,
        final_candidate_count=1,
        first_sequence=1,
        last_sequence=1,
        close_reason="WINDOW_COMPLETE",
        anchor_lower=BASE + timedelta(seconds=50),
        anchor_upper=BASE + timedelta(seconds=51),
    )
    boundary = RevealBoundaryReceipt(
        receipt_id="reveal-boundary-1",
        campaign_id=protocol.campaign_id,
        event_or_market_id="event-1",
        provider_or_authority_id="provider-1",
        boundary_rule_id="PREMATCH_TO_INPLAY",
        source_receipt_id="boundary-source-1",
        source_sha256="9" * 64,
        boundary_lower=BASE + timedelta(seconds=60),
        boundary_upper=BASE + timedelta(seconds=61),
        uncertainty_basis="provider-state",
    )
    return CampaignEvidence(
        protocol=protocol,
        opportunities=(opportunity,),
        cohort_roots=(root,),
        closes=(close,),
        reveal_boundaries=(boundary,),
        authoritative_receipts=(
            AuthoritativeSourceReceipt(
                receipt_id=opportunity.source_receipt_id,
                receipt_sha256=opportunity.source_receipt_sha256,
                campaign_id=protocol.campaign_id,
                opportunity_id=opportunity.opportunity_id,
                universe_rule_result=UniverseResult.ADMITTED,
            ),
        ),
        denominator_sequences=(1,),
        cost_evidence=(CostEvidence(1, True),),
        safety_margin=timedelta(seconds=1),
    )


def _terminal_graph(
    *,
    action_type: str,
    reward_value: str = "1.25",
    revealed_second: int = 120,
    truth: EvidenceTruth = EvidenceTruth.OBSERVED,
):
    observation = Observation(
        environment_id=ENVIRONMENT,
        observed_at=_iso(10),
        available_at=_iso(12),
        evidence=(("source_receipt", "source-receipt-1"),),
    )
    parameters = (
        (("economic_decision_id", "decision-1"), ("paper_ticket_id", "ticket-1"))
        if action_type == "PAPER_PROPOSAL"
        else ()
    )
    action = Action(
        environment_id=ENVIRONMENT,
        observation_id=observation.observation_id,
        action_type=action_type,
        decided_at=_iso(20),
        parameters=parameters,
    )
    simulation = "simulator:v1" if truth is EvidenceTruth.SIMULATED else None
    outcome = Outcome(
        environment_id=ENVIRONMENT,
        action_id=action.action_id,
        revealed_at=_iso(revealed_second),
        truth=truth,
        evidence=(("result", "settled"),),
        simulation_model_id=simulation,
    )
    reward = RewardEvidence(
        environment_id=ENVIRONMENT,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal(reward_value),
        available_at=_iso(revealed_second + 1),
        truth=truth,
        evidence=(("rule", "paper-net-payout-minus-stake-v1"),),
        simulation_model_id=simulation,
    )
    transition = Transition(
        environment_id=ENVIRONMENT,
        episode_id=EPISODE,
        step_index=1,
        observation_id=observation.observation_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at=_iso(revealed_second + 2),
    )
    return observation, action, outcome, reward, transition


def _witness(**changes) -> PaperSettlementLearningWitness:
    graph = _terminal_graph(
        action_type=changes.pop("action_type", "PAPER_PROPOSAL"),
        reward_value=changes.pop("reward_value", "1.25"),
        revealed_second=changes.pop("revealed_second", 120),
        truth=changes.pop("truth", EvidenceTruth.OBSERVED),
    )
    observation, action, outcome, reward, transition = graph
    baseline = EnvironmentCheckpoint(
        environment_id=ENVIRONMENT,
        episode_id=EPISODE,
        policy_id="policy-1",
        step_index=0,
        chain_sha256=hashlib.sha256(b"").hexdigest(),
        last_transition_id=None,
        committed_action_ids=(),
        committed_decision_intents=(),
    )
    next_checkpoint = EnvironmentCheckpoint(
        environment_id=ENVIRONMENT,
        episode_id=EPISODE,
        policy_id="policy-1",
        step_index=1,
        chain_sha256="a" * 64,
        last_transition_id=transition.transition_id,
        committed_action_ids=(action.action_id,),
        committed_decision_intents=(("b" * 64, "c" * 64),),
    )
    values = {
        "ticket_id": "ticket-1",
        "binding_id": "d" * 64,
        "settlement_bundle_sha256": "f" * 64,
        "observation": observation,
        "action": action,
        "outcome": outcome,
        "reward": reward,
        "transition": transition,
        "baseline_checkpoint": baseline,
        "next_checkpoint": next_checkpoint,
    }
    values.update(changes)
    return PaperSettlementLearningWitness(**values)


def _negative_finalization(graph):
    _, action, _, reward, transition = graph
    return PaperAbstentionFinalizationReceipt(
        action_id=action.action_id,
        transition_id=transition.transition_id,
        attribution_id="1" * 64,
        postmortem_id="2" * 64,
        checkpoint_id="3" * 64,
        reward_id=reward.reward_id,
        reward_truth=reward.truth,
    )


def test_settled_action_materializes_exact_sample_and_learning_experience(tmp_path) -> None:
    evidence = _campaign()
    assert verify_campaign(evidence).ok
    witness = _witness()

    sample, experience = admit_settled_action_sample(
        evidence,
        candidate_sequence=1,
        session_id="session-1",
        role=PopulationRole.NEW,
        regime_id="table-tennis:pre-match",
        feature_evidence=_feature(),
        source_rights=_rights(tmp_path),
        witness=witness,
    )

    assert sample.sample_id == "opportunity-1"
    assert sample.source_id == SOURCE
    assert sample.event_id == "event-1"
    assert sample.decision_cutoff == _iso(15)
    assert sample.outcome_available_at == _iso(121)
    assert sample.settled_at == _iso(122)
    assert sample.observed_reward == Decimal("1.25")
    assert sample.terminal_state is TerminalEvidenceState.SETTLED
    assert experience.sample_id == sample.sample_id
    assert experience.source_evidence_sha256 == sample.source_evidence_sha256
    assert experience.action is witness.action
    assert experience.reward is witness.reward
    assert experience.transition is witness.transition


def test_evaluation_roles_never_export_a_learning_experience(tmp_path) -> None:
    common = {
        "evidence": _campaign(),
        "candidate_sequence": 1,
        "session_id": "session-1",
        "regime_id": "table-tennis:pre-match",
        "feature_evidence": _feature(),
        "source_rights": _rights(tmp_path),
        "witness": _witness(),
    }
    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="NEW or REPLAY"):
        admit_settled_action_sample(**common, role=PopulationRole.HOLDOUT)

    sample = admit_settled_evaluation_sample(
        **common,
        role=PopulationRole.HOLDOUT,
    )
    assert type(sample).__name__ == "ForwardPopulationSample"
    assert sample.role is PopulationRole.HOLDOUT
    assert sample.observed_reward == Decimal("1.25")

    with pytest.raises(
        ForwardCampaignAuthorityAdapterError,
        match="RETENTION or HOLDOUT",
    ):
        admit_settled_evaluation_sample(**common, role=PopulationRole.NEW)


def test_finalized_no_bet_stays_negative_without_synthetic_reward(tmp_path) -> None:
    evidence = _campaign(DecisionState.NO_BET)
    graph = _terminal_graph(action_type="NO_BET", reward_value="-0.25")

    sample = admit_negative_sample(
        evidence,
        candidate_sequence=1,
        session_id="session-1",
        role=PopulationRole.REPLAY,
        regime_id="table-tennis:pre-match",
        feature_evidence=_feature(),
        source_rights=_rights(tmp_path),
        observation=graph[0],
        action=graph[1],
        outcome=graph[2],
        reward=graph[3],
        transition=graph[4],
        finalization=_negative_finalization(graph),
    )

    assert sample.decision_state is DecisionState.NO_BET
    assert sample.terminal_state is TerminalEvidenceState.NEGATIVE
    assert sample.observed_reward is None
    assert sample.outcome_available_at == _iso(121)


def test_future_outcome_changes_terminal_hash_not_predecision_source_hash(tmp_path) -> None:
    common = {
        "evidence": _campaign(),
        "candidate_sequence": 1,
        "session_id": "session-1",
        "role": PopulationRole.NEW,
        "regime_id": "table-tennis:pre-match",
        "feature_evidence": _feature(),
        "source_rights": _rights(tmp_path),
    }
    first, _ = admit_settled_action_sample(**common, witness=_witness(reward_value="1"))
    second, _ = admit_settled_action_sample(**common, witness=_witness(reward_value="2"))

    assert first.source_evidence_sha256 == second.source_evidence_sha256
    assert first.terminal_evidence_sha256 != second.terminal_evidence_sha256
    assert first.sample_sha256 != second.sample_sha256


def test_structurally_incomplete_campaign_is_rejected_before_materialization(tmp_path) -> None:
    evidence = replace(_campaign(), denominator_sequences=())

    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="structural verification"):
        admit_settled_action_sample(
            evidence,
            candidate_sequence=1,
            session_id="session-1",
            role=PopulationRole.NEW,
            regime_id="table-tennis:pre-match",
            feature_evidence=_feature(),
            source_rights=_rights(tmp_path),
            witness=_witness(),
        )


def test_source_rights_must_be_checked_by_decision_cutoff(tmp_path) -> None:
    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="checked after"):
        admit_settled_action_sample(
            _campaign(),
            candidate_sequence=1,
            session_id="session-1",
            role=PopulationRole.NEW,
            regime_id="table-tennis:pre-match",
            feature_evidence=_feature(),
            source_rights=_rights(tmp_path, checked_second=16),
            witness=_witness(),
        )


def test_point_in_time_source_must_match_rights_authority(tmp_path) -> None:
    feature = replace(_feature(), source_identity="licensed-provider-account:other")

    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="source differs"):
        admit_settled_action_sample(
            _campaign(),
            candidate_sequence=1,
            session_id="session-1",
            role=PopulationRole.NEW,
            regime_id="table-tennis:pre-match",
            feature_evidence=feature,
            source_rights=_rights(tmp_path),
            witness=_witness(),
        )


def test_paper_action_must_bind_exact_forward_decision(tmp_path) -> None:
    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="does not bind"):
        admit_settled_action_sample(
            _campaign(decision_id="different-decision"),
            candidate_sequence=1,
            session_id="session-1",
            role=PopulationRole.NEW,
            regime_id="table-tennis:pre-match",
            feature_evidence=_feature(),
            source_rights=_rights(tmp_path),
            witness=_witness(),
        )


def test_settled_action_must_be_canonical_paper_proposal(tmp_path) -> None:
    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="PAPER_PROPOSAL"):
        admit_settled_action_sample(
            _campaign(),
            candidate_sequence=1,
            session_id="session-1",
            role=PopulationRole.NEW,
            regime_id="table-tennis:pre-match",
            feature_evidence=_feature(),
            source_rights=_rights(tmp_path),
            witness=_witness(action_type="WAIT"),
        )


def test_simulated_or_pre_boundary_action_outcome_is_rejected(tmp_path) -> None:
    common = {
        "evidence": _campaign(),
        "candidate_sequence": 1,
        "session_id": "session-1",
        "role": PopulationRole.NEW,
        "regime_id": "table-tennis:pre-match",
        "feature_evidence": _feature(),
        "source_rights": _rights(tmp_path),
    }
    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="must be OBSERVED"):
        admit_settled_action_sample(
            **common,
            witness=_witness(truth=EvidenceTruth.SIMULATED),
        )
    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="predates frozen reveal"):
        admit_settled_action_sample(
            **common,
            witness=_witness(revealed_second=60),
        )


def test_transition_decision_time_must_equal_exact_action_time(tmp_path) -> None:
    witness = _witness()
    transition = replace(witness.transition, decision_at=_iso(21))
    checkpoint = replace(
        witness.next_checkpoint,
        last_transition_id=transition.transition_id,
    )
    altered = replace(
        witness,
        transition=transition,
        next_checkpoint=checkpoint,
    )

    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="decision time"):
        admit_settled_action_sample(
            _campaign(),
            candidate_sequence=1,
            session_id="session-1",
            role=PopulationRole.NEW,
            regime_id="table-tennis:pre-match",
            feature_evidence=_feature(),
            source_rights=_rights(tmp_path),
            witness=altered,
        )


def test_negative_finalization_and_action_semantics_are_exact(tmp_path) -> None:
    evidence = _campaign(DecisionState.NO_BET)
    graph = _terminal_graph(action_type="NO_BET")
    bad = replace(_negative_finalization(graph), transition_id="f" * 64)
    common = {
        "evidence": evidence,
        "candidate_sequence": 1,
        "session_id": "session-1",
        "role": PopulationRole.REPLAY,
        "regime_id": "table-tennis:pre-match",
        "feature_evidence": _feature(),
        "source_rights": _rights(tmp_path),
        "observation": graph[0],
        "action": graph[1],
        "outcome": graph[2],
        "reward": graph[3],
        "transition": graph[4],
    }
    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="finalization"):
        admit_negative_sample(**common, finalization=bad)

    wait_graph = _terminal_graph(action_type="WAIT")
    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="requires exact NO_BET"):
        admit_negative_sample(
            **{**common, "observation": wait_graph[0], "action": wait_graph[1],
               "outcome": wait_graph[2], "reward": wait_graph[3],
               "transition": wait_graph[4]},
            finalization=_negative_finalization(wait_graph),
        )

    malformed = replace(
        _negative_finalization(graph),
        attribution_id="not-a-canonical-digest",
    )
    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="lowercase SHA-256"):
        admit_negative_sample(**common, finalization=malformed)


def test_observation_must_belong_to_frozen_opportunity_interval(tmp_path) -> None:
    witness = _witness()
    late_observation = replace(
        witness.observation,
        observed_at=_iso(12),
        available_at=_iso(12),
    )
    late_action = replace(witness.action, observation_id=late_observation.observation_id)
    late_outcome = replace(witness.outcome, action_id=late_action.action_id)
    late_reward = replace(
        witness.reward,
        action_id=late_action.action_id,
        outcome_id=late_outcome.outcome_id,
    )
    late_transition = replace(
        witness.transition,
        observation_id=late_observation.observation_id,
        action_id=late_action.action_id,
        outcome_id=late_outcome.outcome_id,
        reward_id=late_reward.reward_id,
    )
    late_checkpoint = replace(
        witness.next_checkpoint,
        last_transition_id=late_transition.transition_id,
        committed_action_ids=(late_action.action_id,),
    )
    altered = replace(
        witness,
        observation=late_observation,
        action=late_action,
        outcome=late_outcome,
        reward=late_reward,
        transition=late_transition,
        next_checkpoint=late_checkpoint,
    )

    with pytest.raises(ForwardCampaignAuthorityAdapterError, match="outside frozen"):
        admit_settled_action_sample(
            _campaign(),
            candidate_sequence=1,
            session_id="session-1",
            role=PopulationRole.NEW,
            regime_id="table-tennis:pre-match",
            feature_evidence=_feature(),
            source_rights=_rights(tmp_path),
            witness=altered,
        )


def test_pure_adapter_is_restart_deterministic_under_repeated_replay(tmp_path) -> None:
    common = {
        "evidence": _campaign(),
        "candidate_sequence": 1,
        "session_id": "session-1",
        "role": PopulationRole.NEW,
        "regime_id": "table-tennis:pre-match",
        "feature_evidence": _feature(),
        "source_rights": _rights(tmp_path),
        "witness": _witness(),
    }

    baseline_sample, baseline_experience = admit_settled_action_sample(**common)
    for _ in range(250):
        sample, experience = admit_settled_action_sample(**common)
        assert sample.sample_sha256 == baseline_sample.sample_sha256
        assert sample.canonical_payload() == baseline_sample.canonical_payload()
        assert experience == baseline_experience
