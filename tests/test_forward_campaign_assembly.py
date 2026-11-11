from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.continual_learning_cycle import ContinualLearningRecipe, FrozenPolicySuite
from autosport.forward_campaign_assembly import (
    CandidatePopulationAssignment,
    FinalizedNegativeAuthorities,
    ForwardAssemblySessionWindow,
    ForwardCampaignAssemblyError,
    ForwardCandidateAuthorities,
    FrozenForwardPopulationPolicy,
    assemble_forward_campaign,
    restore_forward_campaign_assembly,
)
from autosport.forward_campaign_population import PopulationRole
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
    Action, EnvironmentCheckpoint, EvidenceTruth, Observation, Outcome,
    RewardEvidence, Transition,
)
from autosport.forward_learning_population_binding import (
    bind_forward_learning_populations, verify_forward_learning_population_binding,
)
from autosport.paper_abstention_learning import PaperAbstentionFinalizationReceipt
from autosport.paper_settlement_learning import PaperSettlementLearningWitness
from autosport.point_in_time_evidence import FeatureAvailabilityEvidence
from autosport.policy_evaluation import PolicyEvaluationCase, PolicyRewardMode
from autosport.source_rights_manifest import authorize_source_use, load_source_rights_manifest


BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
SOURCE = "licensed-provider-account:fixture"
ENVIRONMENT = "e" * 64


def _ts(seconds: int) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _sha(value: int) -> str:
    return f"{value:064x}"


def _rights(tmp_path):
    path = tmp_path / "rights.json"
    path.write_text(json.dumps({
        "schema_version": 1, "kind": "autosport_source_rights_manifest",
        "source_identity": SOURCE, "authorized_scopes": ["provider.market_data.read"],
        "privacy_classification": "NON_PERSONAL_DATA",
        "evidence_class": "HUMAN_APPROVED_SOURCE_RIGHTS",
        "terms_reference": "terms:fixture:v1",
        "retention_authority_reference": "retention:fixture:v1",
        "effective_at": "2026-09-01T00:00:00Z", "expires_at": "2026-12-01T00:00:00Z",
        "human_approved": True, "approved_by": "fixture-owner",
        "approval_reference": "fixture-only:not-live-entitlement",
        "approved_at": "2026-08-31T00:00:00Z",
    }), encoding="utf-8")
    return authorize_source_use(
        load_source_rights_manifest(path), source_identity=SOURCE,
        required_scope="provider.market_data.read", at=BASE - timedelta(seconds=5),
    )


def _terminal(sequence, observed, *, negative):
    observation = Observation(
        environment_id=ENVIRONMENT, observed_at=_ts(observed),
        available_at=_ts(observed + 2),
        evidence=(("source_receipt", f"source-{sequence}"),),
    )
    action = Action(
        environment_id=ENVIRONMENT, observation_id=observation.observation_id,
        action_type="NO_BET" if negative else "PAPER_PROPOSAL",
        decided_at=_ts(observed + 10),
        parameters=() if negative else (
            ("economic_decision_id", f"decision-{sequence}"),
            ("paper_ticket_id", f"ticket-{sequence}"),
        ),
    )
    reveal = 120 + (observed // 200) * 200
    outcome = Outcome(
        environment_id=ENVIRONMENT, action_id=action.action_id,
        revealed_at=_ts(reveal), truth=EvidenceTruth.OBSERVED,
        evidence=(("result", f"result-{sequence}"),),
    )
    reward = RewardEvidence(
        environment_id=ENVIRONMENT, action_id=action.action_id,
        outcome_id=outcome.outcome_id, reward=Decimal(sequence - 4),
        available_at=_ts(reveal + 1), truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=ENVIRONMENT, episode_id=_sha(sequence + 1000), step_index=1,
        observation_id=observation.observation_id, action_id=action.action_id,
        outcome_id=outcome.outcome_id, reward_id=reward.reward_id,
        decision_at=action.decided_at, resolved_at=_ts(reveal + 2),
    )
    if negative:
        return FinalizedNegativeAuthorities(
            observation, action, outcome, reward, transition,
            PaperAbstentionFinalizationReceipt(
                action_id=action.action_id, transition_id=transition.transition_id,
                attribution_id=_sha(sequence + 2000), postmortem_id=_sha(sequence + 3000),
                checkpoint_id=_sha(sequence + 4000), reward_id=reward.reward_id,
                reward_truth=EvidenceTruth.OBSERVED,
            ),
        )
    baseline = EnvironmentCheckpoint(
        environment_id=ENVIRONMENT, episode_id=transition.episode_id, policy_id="policy-1",
        step_index=0, chain_sha256=hashlib.sha256(b"").hexdigest(), last_transition_id=None,
        committed_action_ids=(), committed_decision_intents=(),
    )
    next_checkpoint = EnvironmentCheckpoint(
        environment_id=ENVIRONMENT, episode_id=transition.episode_id, policy_id="policy-1",
        step_index=1, chain_sha256=_sha(sequence + 5000),
        last_transition_id=transition.transition_id, committed_action_ids=(action.action_id,),
        committed_decision_intents=((_sha(sequence + 6000), _sha(sequence + 7000)),),
    )
    return PaperSettlementLearningWitness(
        ticket_id=f"ticket-{sequence}", binding_id=_sha(sequence + 8000),
        settlement_bundle_sha256=_sha(sequence + 9000), observation=observation,
        action=action, outcome=outcome, reward=reward, transition=transition,
        baseline_checkpoint=baseline, next_checkpoint=next_checkpoint,
    )


def _inputs(tmp_path, *, session_count=2, late_policy=False, duplicate_source_receipt=False):
    roles = (PopulationRole.NEW, PopulationRole.NEW,
             PopulationRole.RETENTION, PopulationRole.HOLDOUT,
             PopulationRole.REPLAY, PopulationRole.REPLAY,
             PopulationRole.RETENTION, PopulationRole.HOLDOUT)
    assignments = tuple(
        CandidatePopulationAssignment(
            sequence, roles[(sequence - 1) % 8], f"session-{(sequence - 1) // 4 + 1}",
            "table-tennis:pre-match" if sequence % 2 else "table-tennis:live",
        )
        for sequence in range(1, session_count * 4 + 1)
    )
    policy = FrozenForwardPopulationPolicy(
        policy_id="bounded-forward-population-v1", base_evaluation_policy_sha256="c" * 64,
        frozen_at=_ts(2 if late_policy else -2), assignments=assignments,
    )
    protocol = ForwardEvidenceProtocolEnvelope(
        campaign_id="fixture-forward-campaign", scientific_protocol_sha256="a" * 64,
        candidate_universe_rule_id="all-frozen-candidates-v1",
        candidate_universe_rule_sha256="b" * 64,
        forward_evaluation_policy_sha256=policy.policy_sha256,
        runtime_identity_sha256="d" * 64, baseline_set_sha256="e" * 64,
        protective_metric_set_sha256="f" * 64, cost_policy_sha256="0" * 64,
        precommit_anchor_lower=BASE, precommit_anchor_upper=BASE + timedelta(seconds=1),
    )
    rights = _rights(tmp_path)
    opportunities, boundaries, source_receipts = [], [], []
    authorities, roots, windows = [], [], []
    previous = "GENESIS"
    previous_root = "GENESIS"
    for sequence, assignment in enumerate(assignments, start=1):
        session_index = (sequence - 1) // 4
        observed = 10 + session_index * 200 + (sequence - 1) % 4
        negative = sequence % 8 in {2, 4, 6}
        opportunity = ForwardOpportunityEnvelope(
            campaign_id=protocol.campaign_id, protocol_sha256=protocol.protocol_sha256,
            candidate_sequence=sequence, opportunity_id=f"opportunity-{sequence}",
            source_receipt_id=f"source-{sequence}",
            source_receipt_sha256=_sha(
                11 if duplicate_source_receipt and sequence == 3 else sequence + 10
            ),
            causal_cutoff=BASE + timedelta(seconds=observed),
            observed_lower=BASE + timedelta(seconds=observed),
            observed_upper=BASE + timedelta(seconds=observed + 1),
            universe_rule_result=UniverseResult.ADMITTED, universe_rule_reason_code="MATCH",
            provider_acquisition_state="OK", reveal_boundary_receipt_id=f"boundary-{sequence}",
            runtime_identity_sha256=protocol.runtime_identity_sha256,
            predecessor_opportunity_sha256=previous,
            decision_state=DecisionState.NO_BET if negative else DecisionState.ACTION,
            decision_id=None if negative else f"decision-{sequence}",
        )
        opportunities.append(opportunity)
        previous = opportunity.opportunity_sha256
        boundaries.append(RevealBoundaryReceipt(
            receipt_id=f"boundary-{sequence}", campaign_id=protocol.campaign_id,
            event_or_market_id=f"event-{sequence}", provider_or_authority_id="fixture-provider",
            boundary_rule_id="PREMATCH_TO_INPLAY", source_receipt_id=f"reveal-{sequence}",
            source_sha256=_sha(sequence + 20),
            boundary_lower=BASE + timedelta(seconds=60 + session_index * 200),
            boundary_upper=BASE + timedelta(seconds=61 + session_index * 200),
            uncertainty_basis="fixture-state",
        ))
        source_receipts.append(AuthoritativeSourceReceipt(
            receipt_id=opportunity.source_receipt_id,
            receipt_sha256=opportunity.source_receipt_sha256,
            campaign_id=protocol.campaign_id, opportunity_id=opportunity.opportunity_id,
            universe_rule_result=UniverseResult.ADMITTED,
        ))
        feature = FeatureAvailabilityEvidence(
            feature_identity="table-tennis", feature_version="1",
            feature_definition_sha256="1" * 64, source_identity=SOURCE,
            source_revision=_sha(sequence + 30), revision_policy_id="published-before-cutoff",
            dataset_snapshot_id=f"dataset-{sequence}", dataset_record_sha256=_sha(sequence + 40),
            dataset_manifest_sha256=_sha(sequence + 50),
            dataset_lineage_proof_sha256=_sha(sequence + 60),
            feature_provenance_sha256=_sha(sequence + 70),
            feature_payload_sha256=_sha(sequence + 80), as_of_utc=_ts(observed - 1),
            available_at_utc=_ts(observed + 3), decision_cutoff_utc=_ts(observed + 5),
        )
        authorities.append(ForwardCandidateAuthorities(
            sequence, feature, rights, _terminal(sequence, observed, negative=negative)
        ))
        if sequence % 4 == 0:
            root = build_cohort_root(
                tuple(opportunities), previous_cohort_root_sha256=previous_root,
                anchor_lower=BASE + timedelta(seconds=30 + session_index * 200),
                anchor_upper=BASE + timedelta(seconds=31 + session_index * 200),
            )
            roots.append(root)
            previous_root = root.cohort_root_sha256
            windows.append(ForwardAssemblySessionWindow(
                assignment.session_id, _ts(session_index * 200),
                _ts(150 + session_index * 200), _sha(session_index + 100),
            ))
    close = CampaignCloseEnvelope(
        campaign_id=protocol.campaign_id, protocol_sha256=protocol.protocol_sha256,
        terminal_cohort_root_sha256=previous_root, final_candidate_count=len(assignments),
        first_sequence=1, last_sequence=len(assignments), close_reason="WINDOW_COMPLETE",
        anchor_lower=BASE + timedelta(seconds=(session_count - 1) * 200 + 50),
        anchor_upper=BASE + timedelta(seconds=(session_count - 1) * 200 + 51),
    )
    evidence = CampaignEvidence(
        protocol=protocol, opportunities=tuple(opportunities), cohort_roots=tuple(roots),
        closes=(close,), reveal_boundaries=tuple(boundaries),
        authoritative_receipts=tuple(source_receipts),
        denominator_sequences=tuple(range(1, len(assignments) + 1)),
        cost_evidence=tuple(CostEvidence(sequence, True)
                            for sequence in range(1, len(assignments) + 1)),
        safety_margin=timedelta(seconds=1),
    )
    assert verify_campaign(evidence).ok
    return {
        "evidence": evidence, "population_policy": policy,
        "session_windows": tuple(windows), "authorities": tuple(authorities),
        "created_at": _ts(session_count * 200),
    }


def test_exact_denominator_assembly_preserves_all_negative_and_independent_populations(tmp_path):
    inputs = _inputs(tmp_path)
    result = assemble_forward_campaign(**inputs)
    manifest = result.manifest

    assert len(manifest.samples) == 8
    assert len(result.coverage) == 8
    assert tuple(member[0] for member in result.coverage) == tuple(range(1, 9))
    assert {sample.role for sample in manifest.samples} == set(PopulationRole)
    assert len(result.learning_experiences) == 2
    assert {item.sample_id for item in result.learning_experiences} == {
        "opportunity-1", "opportunity-5"
    }
    assert sum(metric.negative_count for session in manifest.sessions
               for metric in session.regime_metrics) == 3
    assert sum(metric.sample_count for session in manifest.sessions
               for metric in session.regime_metrics) == 8
    assert manifest.sessions[1].predecessor_session_sha256 == manifest.sessions[0].session_sha256
    assert result.canonical_payload()["profitability_claim"] is False


@pytest.mark.parametrize("mutation", ("missing-negative", "duplicate", "extra", "wrong-sequence"))
def test_authority_coverage_cannot_drop_duplicate_or_substitute_candidates(tmp_path, mutation):
    inputs = _inputs(tmp_path)
    original = inputs["authorities"]
    if mutation == "missing-negative":
        changed = tuple(item for item in original if item.candidate_sequence != 2)
    elif mutation == "duplicate":
        changed = (*original, original[0])
    elif mutation == "extra":
        changed = (*original, replace(original[0], candidate_sequence=9))
    else:
        changed = (replace(original[0], candidate_sequence=9), *original[1:])
    with pytest.raises(ForwardCampaignAssemblyError, match="coverage|duplicate"):
        assemble_forward_campaign(**{**inputs, "authorities": changed})


def test_post_outcome_population_role_or_regime_switch_breaks_precommit(tmp_path):
    inputs = _inputs(tmp_path)
    policy = inputs["population_policy"]
    swapped = replace(policy.assignments[0], role=PopulationRole.HOLDOUT)
    changed = replace(policy, assignments=(swapped, *policy.assignments[1:]))
    with pytest.raises(ForwardCampaignAssemblyError, match="protocol precommit"):
        assemble_forward_campaign(**{**inputs, "population_policy": changed})
    regime = replace(policy.assignments[0], regime_id="convenient-winner-regime")
    changed = replace(policy, assignments=(regime, *policy.assignments[1:]))
    with pytest.raises(ForwardCampaignAssemblyError, match="protocol precommit"):
        assemble_forward_campaign(**{**inputs, "population_policy": changed})


def test_policy_must_exist_before_anchored_precommit(tmp_path):
    inputs = _inputs(tmp_path, late_policy=True)
    with pytest.raises(ForwardCampaignAssemblyError, match="after precommit"):
        assemble_forward_campaign(**inputs)


@pytest.mark.parametrize("sequence", (True, 1.0, 0, -1))
def test_assignments_reject_coercive_or_nonpositive_sequences(sequence):
    with pytest.raises(ForwardCampaignAssemblyError, match="positive exact int"):
        CandidatePopulationAssignment(sequence, PopulationRole.NEW, "s", "r")


def test_policy_requires_gap_free_ordered_all_role_membership(tmp_path):
    policy = _inputs(tmp_path)["population_policy"]
    with pytest.raises(ForwardCampaignAssemblyError, match="gap-free"):
        replace(policy, assignments=tuple(reversed(policy.assignments)))
    with pytest.raises(ForwardCampaignAssemblyError, match="all four"):
        replace(policy, assignments=tuple(replace(a, role=PopulationRole.NEW)
                                          for a in policy.assignments))


def test_terminal_kind_cannot_convert_negative_to_action_or_action_to_negative(tmp_path):
    inputs = _inputs(tmp_path)
    original = inputs["authorities"]
    for index, terminal, message in ((0, original[1].terminal, "ACTION requires"),
                                      (1, original[0].terminal, "non-action requires")):
        altered = list(original)
        altered[index] = replace(altered[index], terminal=terminal)
        with pytest.raises(ForwardCampaignAssemblyError, match=message):
            assemble_forward_campaign(**{**inputs, "authorities": tuple(altered)})


def test_authority_input_order_is_irrelevant_but_frozen_sequence_is_not(tmp_path):
    inputs = _inputs(tmp_path)
    result = assemble_forward_campaign(**inputs)
    shuffled = assemble_forward_campaign(**{
        **inputs, "authorities": tuple(reversed(inputs["authorities"]))
    })
    assert shuffled.assembly_sha256 == result.assembly_sha256
    assert shuffled.canonical_payload() == result.canonical_payload()


def test_sessions_must_cover_frozen_allocation_and_close_before_assembly(tmp_path):
    inputs = _inputs(tmp_path)
    windows = inputs["session_windows"]
    for changed in ((windows[0], windows[0]),
                    (windows[0], replace(windows[1], session_id="unknown-session"))):
        with pytest.raises(ValueError, match="duplicate|non-empty"):
            assemble_forward_campaign(**{**inputs, "session_windows": changed})
    with pytest.raises(ForwardCampaignAssemblyError, match="predates session close"):
        assemble_forward_campaign(**{**inputs, "created_at": _ts(300)})
    with pytest.raises(ForwardCampaignAssemblyError, match="predates anchored"):
        assemble_forward_campaign(**{**inputs, "created_at": _ts(200)})


def test_same_event_cannot_cross_population_roles(tmp_path):
    inputs = _inputs(tmp_path)
    evidence = inputs["evidence"]
    boundaries = list(evidence.reveal_boundaries)
    boundaries[2] = replace(boundaries[2], event_or_market_id=boundaries[0].event_or_market_id)
    changed = replace(evidence, reveal_boundaries=tuple(boundaries))
    assert verify_campaign(changed).ok
    with pytest.raises(ValueError, match="event identity overlaps"):
        assemble_forward_campaign(**{**inputs, "evidence": changed})


def test_raw_source_record_alias_cannot_cross_population_roles(tmp_path):
    inputs = _inputs(tmp_path)
    authorities = list(inputs["authorities"])
    authorities[2] = replace(
        authorities[2], feature_evidence=replace(
            authorities[2].feature_evidence,
            dataset_record_sha256=authorities[0].feature_evidence.dataset_record_sha256,
        ),
    )
    with pytest.raises(ForwardCampaignAssemblyError, match="raw dataset record identity reused"):
        assemble_forward_campaign(**{**inputs, "authorities": tuple(authorities)})
    inputs = _inputs(tmp_path, duplicate_source_receipt=True)
    with pytest.raises(ForwardCampaignAssemblyError, match="raw source receipt identity reused"):
        assemble_forward_campaign(**inputs)


def test_restart_requires_exact_receipt_and_replays_issued_authorities(tmp_path):
    inputs = _inputs(tmp_path)
    result = assemble_forward_campaign(**inputs)
    payload = json.loads(json.dumps(result.canonical_payload()))
    restored = restore_forward_campaign_assembly(payload, **inputs)
    assert restored.assembly_sha256 == result.assembly_sha256

    for key, value in (("profitability_claim", True), ("unapproved_field", False)):
        changed = {**payload, key: value}
        with pytest.raises(ForwardCampaignAssemblyError, match="restart authority replay"):
            restore_forward_campaign_assembly(changed, **inputs)
    changed = json.loads(json.dumps(payload))
    changed["coverage"][0][0] = True
    with pytest.raises(ForwardCampaignAssemblyError, match="restart authority replay"):
        restore_forward_campaign_assembly(changed, **inputs)


def test_structurally_invalid_campaign_and_raw_duplicate_opportunity_fail_closed(tmp_path):
    inputs = _inputs(tmp_path)
    evidence = inputs["evidence"]
    with pytest.raises(ForwardCampaignAssemblyError, match="structural"):
        assemble_forward_campaign(**{
            **inputs, "evidence": replace(evidence, denominator_sequences=())
        })
    duplicate = replace(
        evidence, opportunities=(*evidence.opportunities, evidence.opportunities[0])
    )
    assert verify_campaign(duplicate).ok
    with pytest.raises(ForwardCampaignAssemblyError, match="coverage"):
        assemble_forward_campaign(**{**inputs, "evidence": duplicate})


def test_endurance_20_sessions_80_candidates_and_fresh_authority_restart(tmp_path):
    inputs = _inputs(tmp_path, session_count=20)
    result = assemble_forward_campaign(**inputs)
    assert len(result.manifest.samples) == 80
    assert len(result.manifest.sessions) == 20
    assert len(result.coverage) == 80
    payload = json.loads(json.dumps(result.canonical_payload()))
    fresh = _inputs(tmp_path, session_count=20)
    assert fresh["authorities"][0].source_rights is not inputs["authorities"][0].source_rights
    restored = restore_forward_campaign_assembly(payload, **fresh)
    assert restored.assembly_sha256 == result.assembly_sha256


def test_assembly_feeds_existing_learning_population_binder_without_role_leak(tmp_path):
    result = assemble_forward_campaign(**_inputs(tmp_path))
    manifest = result.manifest

    def suite(role):
        cases = tuple(PolicyEvaluationCase(
            sample_id=sample.sample_id, observed_at=sample.observed_at,
            reward_available_at=sample.outcome_available_at,
            admissible_actions=("PAPER_PROPOSAL", "WAIT"),
            action_rewards=(("PAPER_PROPOSAL", Decimal("1")), ("WAIT", Decimal("0"))),
            action_costs=(("PAPER_PROPOSAL", Decimal("0")), ("WAIT", Decimal("0"))),
            behavior_propensities=(("PAPER_PROPOSAL", Decimal("0.5")),
                                   ("WAIT", Decimal("0.5"))),
            reward_truth=EvidenceTruth.OBSERVED,
            reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
            source_evidence_sha256=sample.source_evidence_sha256,
            regime_id=sample.regime_id,
            counterfactual_source_id="fixture-mechanical-paper-v1:not-real-forward",
        ) for sample in manifest.samples if sample.role is role)
        return FrozenPolicySuite(role.value.lower(), role.value.lower(), cases)

    retention, holdout = suite(PopulationRole.RETENTION), suite(PopulationRole.HOLDOUT)
    recipe = ContinualLearningRecipe(
        champion_policy_id="f" * 64, retention_suite_sha256=retention.suite_sha256,
        holdout_suite_sha256=holdout.suite_sha256, max_updates=2,
    )
    role_by_sample = {sample.sample_id: sample.role for sample in manifest.samples}
    binding_inputs = {
        "new_experiences": tuple(item for item in result.learning_experiences
                                 if role_by_sample[item.sample_id] is PopulationRole.NEW),
        "replay_experiences": tuple(item for item in result.learning_experiences
                                    if role_by_sample[item.sample_id] is PopulationRole.REPLAY),
        "retention_suite": retention, "holdout_suite": holdout, "recipe": recipe,
    }
    binding = bind_forward_learning_populations(
        manifest, **binding_inputs, bound_at=manifest.created_at,
    )
    assert verify_forward_learning_population_binding(binding, manifest, **binding_inputs)
    assert len(binding.members) == len(result.coverage) == 8
    assert binding.profitability_claim is False
    assert binding.promotion_authorized is False


def test_result_rejects_duplicate_coverage_and_evaluation_training_exports(tmp_path):
    result = assemble_forward_campaign(**_inputs(tmp_path))
    with pytest.raises(ForwardCampaignAssemblyError, match="gap-free"):
        replace(result, coverage=(result.coverage[0], *result.coverage[:-1]))
    with pytest.raises(ForwardCampaignAssemblyError, match="learning members"):
        replace(result, learning_experiences=(*result.learning_experiences,
                                              result.learning_experiences[0]))


def test_restart_in_fresh_python_process_reissues_rights_and_matches_receipt(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(repo / "src"), str(repo / "tests")))}
    setup = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from test_forward_campaign_assembly import _inputs\n"
        "from autosport.forward_campaign_assembly import "
        "assemble_forward_campaign, restore_forward_campaign_assembly\n"
        "inputs = _inputs(Path(sys.argv[1]))\n"
    )
    first = subprocess.run(
        [sys.executable, "-c", setup + "print(json.dumps("
         "assemble_forward_campaign(**inputs).canonical_payload()))", str(tmp_path)],
        cwd=repo, env=env, capture_output=True, text=True, check=True, timeout=30,
    )
    payload = json.loads(first.stdout)
    restarted = subprocess.run(
        [sys.executable, "-c", setup + "print(restore_forward_campaign_assembly("
         "json.load(sys.stdin), **inputs).assembly_sha256)",
         str(tmp_path)],
        input=first.stdout, cwd=repo, env=env, capture_output=True,
        text=True, check=True, timeout=30,
    )
    assert restarted.stdout.strip() == payload["assembly_sha256"]
