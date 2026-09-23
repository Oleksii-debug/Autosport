import hashlib
import json
from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.agent_loop import (
    AgentLoopError,
    AgentLoopPhase,
    AgentLoopRuntime,
    AttributionComponent,
    AttributionFinding,
    AttributionStatus,
    ConflictingAgentLoopEvidenceError,
    ExternalEffectState,
    OutcomeAttribution,
    ReflectionPostmortem,
)
from autosport.learning_environment import (
    Action,
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)
from autosport.research_supervisor import ResearchSupervisor
from autosport.scientific_registry import ScientificRegistry


SOURCE_SHA = "1" * 64
CONFIG_SHA = "2" * 64
GOAL_SHA = "3" * 64
RISK_SHA = "4" * 64
EVIDENCE_SHA = "5" * 64
RANDOMNESS_SHA = "6" * 64


def json_load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_sha256(value):
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rewrite_with_valid_state_digest(path, state):
    unsigned = {key: value for key, value in state.items() if key != "state_sha256"}
    state["state_sha256"] = canonical_sha256(unsigned)
    path.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _environment():
    identity = EnvironmentIdentity(
        source_id="paper-source-v1",
        config_id="agent-loop-config-v1",
        data_id="causal-dataset-v1",
        protocol_id="agent-loop-protocol-v1",
        cutoff_ts="2026-09-19T14:00:00Z",
        seed=17,
    )
    return CausalLearningEnvironment(
        identity,
        episode_key="agent-loop-episode-1",
        policy_id="policy-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )


def _runtime(tmp_path, environment):
    return AgentLoopRuntime.initialize_pristine(
        tmp_path / "agent-loop.json",
        loop_id="loop-1",
        environment_checkpoint=environment.checkpoint(),
        policy_id="policy-v1",
        economic_goal_fingerprint=GOAL_SHA,
        risk_fingerprint=RISK_SHA,
        source_sha256=SOURCE_SHA,
        config_sha256=CONFIG_SHA,
        at="2026-09-19T13:00:00Z",
    )


def _observation(environment, *, suffix="1"):
    return Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-19T13:00:00Z",
        available_at="2026-09-19T13:00:01Z",
        evidence=(("market_state", f"snapshot-{suffix}"),),
    )


def _advance_to_action(runtime):
    runtime.advance(
        expected=AgentLoopPhase.OBSERVE,
        at="2026-09-19T13:00:02Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.ASSESS,
        at="2026-09-19T13:00:03Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.PLAN,
        at="2026-09-19T13:00:04Z",
    )
    return runtime.advance(
        expected=AgentLoopPhase.DECIDE,
        at="2026-09-19T13:00:05Z",
    )


def _resolve(environment, action, *, truth=EvidenceTruth.OBSERVED):
    model_id = "sim-model-v1" if truth is EvidenceTruth.SIMULATED else None
    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at="2026-09-19T13:05:00Z",
        truth=truth,
        evidence=(("settlement", "paper-result"),),
        simulation_model_id=model_id,
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at="2026-09-19T13:05:01Z",
        truth=truth,
        evidence=(("reward_rule", "paper-economic-reward-v1"),),
        simulation_model_id=model_id,
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at="2026-09-19T13:05:02Z",
    )
    return outcome, reward, transition


def _attribution(environment, transition, outcome, reward, **overrides):
    values = {
        "environment_id": environment.environment_id,
        "episode_id": environment.episode.episode_id,
        "transition_id": transition.transition_id,
        "action_id": transition.action_id,
        "outcome_id": outcome.outcome_id,
        "reward_id": reward.reward_id,
        "reward_value": reward.reward,
        "truth": reward.truth,
        "simulation_model_id": reward.simulation_model_id,
        "attributed_at": "2026-09-19T13:05:04Z",
        "findings": (
            AttributionFinding(
                component=AttributionComponent.FORECAST,
                status=AttributionStatus.SUPPORTED,
                evidence_sha256=EVIDENCE_SHA,
                evidence_available_at="2026-09-19T13:05:03Z",
                contribution=Decimal("0.10"),
                reason_code="CALIBRATED_FORECAST_EVIDENCE",
            ),
            AttributionFinding(
                component=AttributionComponent.RANDOMNESS,
                status=AttributionStatus.UNKNOWN,
                evidence_sha256=RANDOMNESS_SHA,
                evidence_available_at="2026-09-19T13:05:03Z",
                reason_code="UNRESOLVED_RANDOMNESS",
            ),
        ),
    }
    values.update(overrides)
    return OutcomeAttribution(**values)



def _rewrite_pristine_as_integrated_v2(path):
    state = json_load(path)
    state["schema_version"] = 2
    state.pop("checkpoint_history")
    rewrite_with_valid_state_digest(path, state)


def _legacy_v2_runtime_through_attribution(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    _rewrite_pristine_as_integrated_v2(runtime.path)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    outcome, reward, transition = _resolve(environment, action)
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )
    attribution = _attribution(environment, transition, outcome, reward)
    runtime.record_attribution(
        attribution,
        at="2026-09-19T13:05:04Z",
    )
    assert runtime.snapshot().phase is AgentLoopPhase.REFLECT
    return runtime


def test_schema_v2_restart_upgrades_only_from_canonical_checkpoint(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    _rewrite_pristine_as_integrated_v2(runtime.path)
    legacy_bytes = runtime.path.read_bytes()

    reopened = AgentLoopRuntime(runtime.path)
    assert reopened.snapshot().phase is AgentLoopPhase.BOOTSTRAP
    assert runtime.path.read_bytes() == legacy_bytes
    assert json_load(runtime.path)["schema_version"] == 2

    observation = _observation(environment)
    reopened.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(reopened)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    reopened.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    legacy_decision = json_load(runtime.path)["decisions"][0]
    assert "decision_intent_id" not in legacy_decision
    assert "decision_payload_id" not in legacy_decision

    outcome, reward, transition = _resolve(environment, action)
    reopened.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )
    reopened.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )
    attribution = _attribution(environment, transition, outcome, reward)
    reopened.record_attribution(
        attribution,
        at="2026-09-19T13:05:04Z",
    )
    postmortem = ReflectionPostmortem(
        attribution_id=attribution.attribution_id,
        transition_id=transition.transition_id,
        created_at="2026-09-19T13:05:05Z",
        unresolved_components=(AttributionComponent.RANDOMNESS,),
        summary_code="LEGACY_V2_CHECKPOINT_UPGRADE",
    )
    reopened.record_postmortem(
        postmortem,
        at="2026-09-19T13:05:05Z",
    )
    before_upgrade = json_load(runtime.path)
    assert before_upgrade["schema_version"] == 2
    assert "checkpoint_history" not in before_upgrade

    first_checkpoint = environment.checkpoint()
    upgraded = reopened.commit_checkpoint(
        first_checkpoint,
        at="2026-09-19T13:05:06Z",
    )
    upgraded_state = json_load(runtime.path)
    assert upgraded_state["schema_version"] == 3
    assert upgraded_state["checkpoint_history"][0]["checkpoint_id"] == (
        first_checkpoint.checkpoint_id
    )
    upgraded_decision = upgraded_state["decisions"][0]
    assert upgraded_decision["decision_intent_id"]
    assert upgraded_decision["decision_payload_id"]
    assert upgraded_decision["parameters"] is None
    assert upgraded.phase is AgentLoopPhase.CHECKPOINT

    second_observation = _observation(environment, suffix="2")
    reopened.begin_observation(
        second_observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:05:07Z",
    )
    reopened.advance(expected=AgentLoopPhase.OBSERVE, at="2026-09-19T13:05:08Z")
    reopened.advance(expected=AgentLoopPhase.ASSESS, at="2026-09-19T13:05:09Z")
    reopened.advance(expected=AgentLoopPhase.PLAN, at="2026-09-19T13:05:10Z")
    reopened.advance(expected=AgentLoopPhase.DECIDE, at="2026-09-19T13:05:11Z")
    second_action = environment.act(
        second_observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:05:11Z",
    )
    reopened.commit_action(
        second_action,
        episode=environment.episode,
        observation=second_observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:05:11Z",
    )
    second_outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=second_action.action_id,
        revealed_at="2026-09-19T13:10:00Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("settlement", "paper-result-2"),),
    )
    second_reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=second_action.action_id,
        outcome_id=second_outcome.outcome_id,
        reward=Decimal("0.10"),
        available_at="2026-09-19T13:10:01Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("reward_rule", "paper-economic-reward-v1"),),
    )
    second_transition = environment.resolve(
        second_action.action_id,
        outcome=second_outcome,
        reward=second_reward,
        resolved_at="2026-09-19T13:10:02Z",
    )
    reopened.record_resolution(
        second_transition,
        outcome=second_outcome,
        reward=second_reward,
        at="2026-09-19T13:10:02Z",
    )
    reopened.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:10:03Z",
    )
    second_attribution = _attribution(
        environment,
        second_transition,
        second_outcome,
        second_reward,
        attributed_at="2026-09-19T13:10:04Z",
    )
    reopened.record_attribution(
        second_attribution,
        at="2026-09-19T13:10:04Z",
    )
    second_postmortem = ReflectionPostmortem(
        attribution_id=second_attribution.attribution_id,
        transition_id=second_transition.transition_id,
        created_at="2026-09-19T13:10:05Z",
        unresolved_components=(AttributionComponent.RANDOMNESS,),
        summary_code="POST_UPGRADE_V3_CHECKPOINT",
    )
    reopened.record_postmortem(
        second_postmortem,
        at="2026-09-19T13:10:05Z",
    )
    second_checkpoint = environment.checkpoint()
    reopened.commit_checkpoint(
        second_checkpoint,
        at="2026-09-19T13:10:06Z",
    )
    final_state = json_load(runtime.path)
    assert [item["step_index"] for item in final_state["checkpoint_history"]] == [
        1,
        2,
    ]
    assert final_state["checkpoint_history"][-1]["checkpoint_id"] == (
        second_checkpoint.checkpoint_id
    )


def test_schema_v2_self_consistent_malformed_state_fails_closed(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    _rewrite_pristine_as_integrated_v2(runtime.path)
    malformed = json_load(runtime.path)
    malformed["decisions"] = {}
    rewrite_with_valid_state_digest(runtime.path, malformed)

    with pytest.raises(AgentLoopError, match="decisions must be a list"):
        AgentLoopRuntime(runtime.path)


def test_schema_v2_self_consistent_orphaned_current_attribution_fails_closed(
    tmp_path,
):
    runtime = _legacy_v2_runtime_through_attribution(tmp_path)
    malformed = json_load(runtime.path)
    malformed["current"]["attribution_id"] = "f" * 64
    rewrite_with_valid_state_digest(runtime.path, malformed)

    with pytest.raises(
        AgentLoopError,
        match="current attribution does not bind resolution",
    ):
        AgentLoopRuntime(runtime.path)


def test_schema_v2_self_consistent_resolution_cross_link_fails_closed(tmp_path):
    runtime = _legacy_v2_runtime_through_attribution(tmp_path)
    malformed = json_load(runtime.path)
    malformed["resolutions"][0]["action_id"] = "e" * 64
    rewrite_with_valid_state_digest(runtime.path, malformed)

    with pytest.raises(
        AgentLoopError,
        match="resolution does not bind a durable decision",
    ):
        AgentLoopRuntime(runtime.path)


def test_schema_v2_self_consistent_effect_and_phase_rewrites_fail_closed(tmp_path):
    runtime = _legacy_v2_runtime_through_attribution(tmp_path)
    valid = json_load(runtime.path)

    wrong_effect = json.loads(json.dumps(valid))
    wrong_effect["external_effect_state"] = (
        ExternalEffectState.UNKNOWN_EXTERNAL_EFFECT.value
    )
    rewrite_with_valid_state_digest(runtime.path, wrong_effect)
    with pytest.raises(
        AgentLoopError,
        match="current external effect differs from decision",
    ):
        AgentLoopRuntime(runtime.path)

    wrong_phase = json.loads(json.dumps(valid))
    wrong_phase["phase"] = AgentLoopPhase.WAIT_OUTCOME.value
    rewrite_with_valid_state_digest(runtime.path, wrong_phase)
    with pytest.raises(
        AgentLoopError,
        match="WAIT_OUTCOME current evidence mismatch",
    ):
        AgentLoopRuntime(runtime.path)


def test_full_paper_loop_attribution_research_handoff_and_restart(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)

    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    assert _advance_to_action(runtime).phase is AgentLoopPhase.ACT_OR_ABSTAIN

    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    commit = runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    assert commit.newly_committed is True
    assert commit.may_execute is True
    assert runtime.snapshot().phase is AgentLoopPhase.WAIT_OUTCOME

    outcome, reward, transition = _resolve(environment, action)
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )

    attribution = _attribution(
        environment,
        transition,
        outcome,
        reward,
    )
    runtime.record_attribution(
        attribution,
        at="2026-09-19T13:05:04Z",
    )
    postmortem = ReflectionPostmortem(
        attribution_id=attribution.attribution_id,
        transition_id=transition.transition_id,
        created_at="2026-09-19T13:05:05Z",
        unresolved_components=(AttributionComponent.RANDOMNESS,),
        summary_code="UNRESOLVED_RANDOMNESS_REQUIRES_RESEARCH",
        research_question_statement=(
            "Does the unresolved randomness component remain material under the "
            "frozen causal paper protocol?"
        ),
    )
    runtime.record_postmortem(
        postmortem,
        at="2026-09-19T13:05:05Z",
    )

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json",
        registry,
    )
    run = runtime.handoff_research(
        supervisor,
        budget_units=8,
        deadline_at="2026-09-20T13:05:05Z",
        at="2026-09-19T13:05:06Z",
    )
    assert len(supervisor.list_runs()) == 1
    assert registry.get("ResearchQuestion", run.question_id) is not None
    stored_handoff = json_load(runtime.path)["research_handoffs"][0]
    assert stored_handoff["trigger_id"] == run.trigger_id
    assert stored_handoff["source_event_identity_sha256"]
    assert stored_handoff["receipt_sha256"]

    checkpoint = environment.checkpoint()
    final = runtime.commit_checkpoint(
        checkpoint,
        at="2026-09-19T13:05:07Z",
    )
    reopened = AgentLoopRuntime(runtime.path).snapshot()

    assert reopened == final
    assert reopened.phase is AgentLoopPhase.CHECKPOINT
    assert reopened.environment_checkpoint_id == checkpoint.checkpoint_id
    assert reopened.attribution_id == attribution.attribution_id
    assert reopened.postmortem_id == postmortem.postmortem_id
    assert reopened.research_run_id == run.run_id

    duplicate = runtime.handoff_research(
        supervisor,
        budget_units=8,
        deadline_at="2026-09-20T13:05:05Z",
        at="2026-09-19T13:05:08Z",
    )
    assert duplicate.run_id == run.run_id
    assert len(supervisor.list_runs()) == 1


def test_post_cutoff_observation_cannot_bypass_environment_authority(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    post_cutoff = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-19T14:00:01Z",
        available_at="2026-09-19T14:00:02Z",
        evidence=(("market_state", "post-cutoff"),),
    )

    with pytest.raises(
        AgentLoopError,
        match="canonical environment evidence cutoff",
    ):
        runtime.begin_observation(
            post_cutoff,
            environment_identity=environment.identity,
            at="2026-09-19T14:00:03Z",
        )

    snapshot = runtime.snapshot()
    assert snapshot.phase is AgentLoopPhase.BOOTSTRAP
    assert snapshot.observation_id is None


def test_restart_never_reauthorizes_same_action(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at="2026-09-19T13:00:05Z",
        parameters=(("candidate_id", "candidate-1"),),
    )
    first = runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.PAPER_ONLY,
        at="2026-09-19T13:00:05Z",
    )
    assert first.newly_committed and first.may_execute

    reopened = AgentLoopRuntime(runtime.path)
    retry = reopened.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.PAPER_ONLY,
        at="2026-09-19T13:00:06Z",
    )
    assert retry.newly_committed is False
    assert retry.may_execute is False


def test_direct_action_cannot_bypass_episode_admissible_set(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)

    unauthorized = Action(
        environment_id=environment.environment_id,
        observation_id=observation.observation_id,
        action_type="UNAUTHORIZED",
        decided_at="2026-09-19T13:00:05Z",
    )
    with pytest.raises(
        AgentLoopError,
        match="canonical episode admissible set",
    ):
        runtime.commit_action(
            unauthorized,
            episode=environment.episode,
            observation=observation,
            effect_state=ExternalEffectState.NONE,
            at="2026-09-19T13:00:05Z",
        )

    snapshot = runtime.snapshot()
    assert snapshot.phase is AgentLoopPhase.ACT_OR_ABSTAIN
    assert snapshot.action_id is None


def test_unknown_external_effect_fails_closed_until_reconciled(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at="2026-09-19T13:00:05Z",
    )
    committed = runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.UNKNOWN_EXTERNAL_EFFECT,
        at="2026-09-19T13:00:05Z",
    )
    assert committed.may_execute is False
    assert (
        runtime.snapshot().phase
        is AgentLoopPhase.UNKNOWN_EXTERNAL_EFFECT
    )

    retry = AgentLoopRuntime(runtime.path).commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.UNKNOWN_EXTERNAL_EFFECT,
        at="2026-09-19T13:00:06Z",
    )
    assert retry.may_execute is False
    assert retry.newly_committed is False

    recovered = runtime.reconcile_unknown_external_effect(
        action_id=action.action_id,
        reconciled_as=ExternalEffectState.PAPER_ONLY,
        at="2026-09-19T13:00:07Z",
    )
    assert recovered.phase is AgentLoopPhase.WAIT_OUTCOME


def test_unknown_external_effect_cannot_rewrite_resolved_or_checkpointed_action(
    tmp_path,
):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    outcome, reward, transition = _resolve(environment, action)
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )

    resolved_state = json_load(runtime.path)
    assert runtime.snapshot().phase is AgentLoopPhase.EVALUATE
    with pytest.raises(
        AgentLoopError,
        match="unknown external effect marking requires WAIT_OUTCOME",
    ):
        runtime.mark_unknown_external_effect(
            action_id=action.action_id,
            at="2026-09-19T13:05:03Z",
        )
    assert json_load(runtime.path) == resolved_state

    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )
    attribution = _attribution(
        environment,
        transition,
        outcome,
        reward,
    )
    runtime.record_attribution(
        attribution,
        at="2026-09-19T13:05:04Z",
    )
    runtime.record_postmortem(
        ReflectionPostmortem(
            attribution_id=attribution.attribution_id,
            transition_id=transition.transition_id,
            created_at="2026-09-19T13:05:05Z",
            unresolved_components=(),
            summary_code="NO_RESEARCH_REQUIRED",
        ),
        at="2026-09-19T13:05:05Z",
    )
    runtime.commit_checkpoint(
        environment.checkpoint(),
        at="2026-09-19T13:05:06Z",
    )

    checkpointed_state = json_load(runtime.path)
    assert runtime.snapshot().phase is AgentLoopPhase.CHECKPOINT
    with pytest.raises(
        AgentLoopError,
        match="unknown external effect marking requires WAIT_OUTCOME",
    ):
        runtime.mark_unknown_external_effect(
            action_id=action.action_id,
            at="2026-09-19T13:05:07Z",
        )
    assert json_load(runtime.path) == checkpointed_state


def test_next_observation_requires_durable_current_transition_checkpoint(
    tmp_path,
):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    outcome, reward, transition = _resolve(environment, action)
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )
    attribution = _attribution(
        environment,
        transition,
        outcome,
        reward,
    )
    runtime.record_attribution(
        attribution,
        at="2026-09-19T13:05:04Z",
    )
    runtime.record_postmortem(
        ReflectionPostmortem(
            attribution_id=attribution.attribution_id,
            transition_id=transition.transition_id,
            created_at="2026-09-19T13:05:05Z",
            unresolved_components=(),
            summary_code="NO_RESEARCH_REQUIRED",
        ),
        at="2026-09-19T13:05:05Z",
    )

    pending = json_load(runtime.path)
    assert runtime.snapshot().phase is AgentLoopPhase.CHECKPOINT
    assert runtime.snapshot().checkpointed_transition_id is None

    forged_checkpoint_head = json.loads(json.dumps(pending))
    forged_checkpoint_head["checkpointed_transition_id"] = transition.transition_id
    rewrite_with_valid_state_digest(runtime.path, forged_checkpoint_head)
    with pytest.raises(
        AgentLoopError,
        match="checkpoint head differs from latest durable checkpoint",
    ):
        AgentLoopRuntime(runtime.path)
    rewrite_with_valid_state_digest(runtime.path, pending)
    assert AgentLoopRuntime(runtime.path).snapshot().phase is AgentLoopPhase.CHECKPOINT

    next_observation = _observation(environment, suffix="2")
    with pytest.raises(
        AgentLoopError,
        match="new observation requires durable checkpoint for current transition",
    ):
        runtime.begin_observation(
            next_observation,
            environment_identity=environment.identity,
            at="2026-09-19T13:05:06Z",
        )
    assert json_load(runtime.path) == pending

    recovered = AgentLoopRuntime(runtime.path)
    with pytest.raises(
        AgentLoopError,
        match="new observation requires durable checkpoint for current transition",
    ):
        recovered.begin_observation(
            next_observation,
            environment_identity=environment.identity,
            at="2026-09-19T13:05:07Z",
        )
    assert json_load(runtime.path) == pending

    checkpoint = environment.checkpoint()
    forged_checkpoint = replace(checkpoint, chain_sha256="f" * 64)
    before_forged_commit = recovered.path.read_bytes()
    with pytest.raises(
        AgentLoopError,
        match="checkpoint chain does not bind durable transition history",
    ):
        recovered.commit_checkpoint(
            forged_checkpoint,
            at="2026-09-19T13:05:08Z",
        )
    assert recovered.path.read_bytes() == before_forged_commit
    assert AgentLoopRuntime(recovered.path).snapshot().phase is AgentLoopPhase.CHECKPOINT

    committed = recovered.commit_checkpoint(
        checkpoint,
        at="2026-09-19T13:05:09Z",
    )
    assert committed.checkpointed_transition_id == transition.transition_id
    committed_state = json_load(runtime.path)
    assert committed_state["checkpoint_history"][-1]["checkpoint_id"] == checkpoint.checkpoint_id
    assert (
        committed_state["checkpoint_history"][-1]["last_transition_id"]
        == transition.transition_id
    )
    started = recovered.begin_observation(
        next_observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:05:10Z",
    )
    assert started.phase is AgentLoopPhase.OBSERVE
    assert started.transition_id is None
    assert started.checkpointed_transition_id == transition.transition_id


def test_future_evidence_and_observed_simulated_relabel_fail_closed(
    tmp_path,
):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    outcome, reward, transition = _resolve(
        environment,
        action,
        truth=EvidenceTruth.SIMULATED,
    )
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )

    future = _attribution(
        environment,
        transition,
        outcome,
        reward,
        findings=(
            AttributionFinding(
                component=AttributionComponent.RANDOMNESS,
                status=AttributionStatus.UNKNOWN,
                evidence_sha256=RANDOMNESS_SHA,
                evidence_available_at="2026-09-19T13:06:00Z",
                reason_code="FUTURE_EVIDENCE",
            ),
        ),
    )
    with pytest.raises(AgentLoopError, match="future evidence"):
        runtime.record_attribution(
            future,
            at="2026-09-19T13:05:04Z",
        )

    relabelled = _attribution(
        environment,
        transition,
        outcome,
        reward,
        truth=EvidenceTruth.OBSERVED,
        simulation_model_id=None,
    )
    with pytest.raises(AgentLoopError, match="relabel"):
        runtime.record_attribution(
            relabelled,
            at="2026-09-19T13:05:04Z",
        )


def test_attribution_must_bind_exact_reward_and_unknown_cannot_invent_credit(
    tmp_path,
):
    with pytest.raises(AgentLoopError, match="UNKNOWN attribution"):
        AttributionFinding(
            component=AttributionComponent.RANDOMNESS,
            status=AttributionStatus.UNKNOWN,
            evidence_sha256=RANDOMNESS_SHA,
            evidence_available_at="2026-09-19T13:05:03Z",
            contribution=Decimal("1"),
            reason_code="INVENTED_CREDIT",
        )

    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    outcome, reward, transition = _resolve(environment, action)
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )
    wrong_reward = _attribution(
        environment,
        transition,
        outcome,
        reward,
        reward_value=Decimal("99"),
    )
    with pytest.raises(AgentLoopError, match="RewardEvidence"):
        runtime.record_attribution(
            wrong_reward,
            at="2026-09-19T13:05:04Z",
        )


def test_pause_recovery_preserves_exact_phase_and_authority_fingerprints(
    tmp_path,
):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    paused = runtime.pause(at="2026-09-19T13:00:02Z")
    assert paused.phase is AgentLoopPhase.PAUSED
    assert paused.resume_phase is AgentLoopPhase.OBSERVE

    assert (
        runtime.resume(at="2026-09-19T13:00:03Z").phase
        is AgentLoopPhase.RECOVERY
    )
    recovered = runtime.recover(at="2026-09-19T13:00:04Z")
    assert recovered.phase is AgentLoopPhase.OBSERVE
    assert recovered.economic_goal_fingerprint == GOAL_SHA
    assert recovered.risk_fingerprint == RISK_SHA

    with pytest.raises(ConflictingAgentLoopEvidenceError):
        AgentLoopRuntime.initialize_pristine(
            runtime.path,
            loop_id="loop-1",
            environment_checkpoint=environment.checkpoint(),
            policy_id="policy-v1",
            economic_goal_fingerprint="7" * 64,
            risk_fingerprint=RISK_SHA,
            source_sha256=SOURCE_SHA,
            config_sha256=CONFIG_SHA,
            at="2026-09-19T13:00:05Z",
        )


def test_checkpoint_rejects_stale_environment_identity(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    other = CausalLearningEnvironment(
        EnvironmentIdentity(
            source_id="paper-source-v1",
            config_id="agent-loop-config-v1",
            data_id="other-dataset",
            protocol_id="agent-loop-protocol-v1",
            cutoff_ts="2026-09-19T14:00:00Z",
            seed=17,
        ),
        episode_key="agent-loop-episode-1",
        policy_id="policy-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    outcome, reward, transition = _resolve(environment, action)
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )
    attribution = _attribution(
        environment,
        transition,
        outcome,
        reward,
    )
    runtime.record_attribution(
        attribution,
        at="2026-09-19T13:05:04Z",
    )
    runtime.record_postmortem(
        ReflectionPostmortem(
            attribution_id=attribution.attribution_id,
            transition_id=transition.transition_id,
            created_at="2026-09-19T13:05:05Z",
            unresolved_components=(),
            summary_code="NO_RESEARCH_REQUIRED",
        ),
        at="2026-09-19T13:05:05Z",
    )
    with pytest.raises(AgentLoopError, match="environment mismatch"):
        runtime.commit_checkpoint(
            other.checkpoint(),
            at="2026-09-19T13:05:06Z",
        )


def test_attribution_and_postmortem_identity_keys_are_causal_and_immutable():
    from dataclasses import replace

    finding = AttributionFinding(
        component=AttributionComponent.RANDOMNESS,
        status=AttributionStatus.UNKNOWN,
        evidence_sha256=RANDOMNESS_SHA,
        evidence_available_at="2026-09-19T13:05:03Z",
        reason_code="UNRESOLVED",
    )
    base = OutcomeAttribution(
        environment_id="a" * 64,
        episode_id="b" * 64,
        transition_id="c" * 64,
        action_id="d" * 64,
        outcome_id="e" * 64,
        reward_id="f" * 64,
        reward_value=Decimal("0.25"),
        truth=EvidenceTruth.OBSERVED,
        simulation_model_id=None,
        attributed_at="2026-09-19T13:05:04Z",
        findings=(finding,),
    )
    changed_payload = replace(
        base,
        attributed_at="2026-09-19T13:05:05Z",
        findings=(
            replace(finding, reason_code="DIFFERENT_EVIDENCE"),
        ),
    )
    assert base.attribution_id == changed_payload.attribution_id

    postmortem = ReflectionPostmortem(
        attribution_id=base.attribution_id,
        transition_id=base.transition_id,
        created_at="2026-09-19T13:05:06Z",
        unresolved_components=(AttributionComponent.RANDOMNESS,),
        summary_code="FIRST",
    )
    changed_postmortem = replace(
        postmortem,
        created_at="2026-09-19T13:05:07Z",
        summary_code="SECOND",
    )
    assert postmortem.postmortem_id == changed_postmortem.postmortem_id

    from autosport.agent_loop import CreditAssignment

    assert CreditAssignment is AttributionFinding

def test_resolution_rejects_forged_environment_decision_and_time_evidence(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )

    foreign_outcome = Outcome(
        environment_id="f" * 64,
        action_id=action.action_id,
        revealed_at="2026-09-19T13:05:00Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("settlement", "foreign"),),
    )
    foreign_reward = RewardEvidence(
        environment_id="f" * 64,
        action_id=action.action_id,
        outcome_id=foreign_outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at="2026-09-19T13:05:01Z",
        truth=EvidenceTruth.OBSERVED,
    )
    foreign_transition = Transition(
        environment_id=environment.environment_id,
        episode_id=environment.episode.episode_id,
        step_index=1,
        observation_id=observation.observation_id,
        action_id=action.action_id,
        outcome_id=foreign_outcome.outcome_id,
        reward_id=foreign_reward.reward_id,
        decision_at=action.decided_at,
        resolved_at="2026-09-19T13:05:02Z",
    )
    with pytest.raises(
        AgentLoopError,
        match="another AgentLoop environment",
    ):
        runtime.record_resolution(
            foreign_transition,
            outcome=foreign_outcome,
            reward=foreign_reward,
            at="2026-09-19T13:05:02Z",
        )

    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at="2026-09-19T13:05:00Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("settlement", "paper-result"),),
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at="2026-09-19T13:05:01Z",
        truth=EvidenceTruth.OBSERVED,
    )
    wrong_observation = Transition(
        environment_id=environment.environment_id,
        episode_id=environment.episode.episode_id,
        step_index=1,
        observation_id="a" * 64,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at="2026-09-19T13:05:02Z",
    )
    with pytest.raises(AgentLoopError, match="durable current observation"):
        runtime.record_resolution(
            wrong_observation,
            outcome=outcome,
            reward=reward,
            at="2026-09-19T13:05:02Z",
        )

    wrong_decision = Transition(
        environment_id=environment.environment_id,
        episode_id=environment.episode.episode_id,
        step_index=1,
        observation_id=observation.observation_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward_id=reward.reward_id,
        decision_at="2026-09-19T13:00:06Z",
        resolved_at="2026-09-19T13:05:02Z",
    )
    with pytest.raises(AgentLoopError, match="durable action decision"):
        runtime.record_resolution(
            wrong_decision,
            outcome=outcome,
            reward=reward,
            at="2026-09-19T13:05:02Z",
        )

    early_outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at="2026-09-19T13:00:04Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("settlement", "too-early"),),
    )
    early_reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=early_outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at="2026-09-19T13:00:04Z",
        truth=EvidenceTruth.OBSERVED,
    )
    early_transition = Transition(
        environment_id=environment.environment_id,
        episode_id=environment.episode.episode_id,
        step_index=1,
        observation_id=observation.observation_id,
        action_id=action.action_id,
        outcome_id=early_outcome.outcome_id,
        reward_id=early_reward.reward_id,
        decision_at=action.decided_at,
        resolved_at="2026-09-19T13:05:02Z",
    )
    with pytest.raises(AgentLoopError, match="predates action decision"):
        runtime.record_resolution(
            early_transition,
            outcome=early_outcome,
            reward=early_reward,
            at="2026-09-19T13:05:02Z",
        )

    future_transition = Transition(
        environment_id=environment.environment_id,
        episode_id=environment.episode.episode_id,
        step_index=1,
        observation_id=observation.observation_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at="2026-09-19T13:06:00Z",
    )
    with pytest.raises(AgentLoopError, match="future resolution evidence"):
        runtime.record_resolution(
            future_transition,
            outcome=outcome,
            reward=reward,
            at="2026-09-19T13:05:02Z",
        )

    assert runtime.snapshot().phase is AgentLoopPhase.WAIT_OUTCOME
    assert runtime.snapshot().transition_id is None


def test_postmortem_cannot_relabel_supported_attribution_as_unresolved(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    outcome, reward, transition = _resolve(environment, action)
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )
    attribution = _attribution(
        environment,
        transition,
        outcome,
        reward,
    )
    runtime.record_attribution(
        attribution,
        at="2026-09-19T13:05:04Z",
    )

    forged = ReflectionPostmortem(
        attribution_id=attribution.attribution_id,
        transition_id=transition.transition_id,
        created_at="2026-09-19T13:05:05Z",
        unresolved_components=(AttributionComponent.FORECAST,),
        summary_code="SUPPORTED_COMPONENT_IS_NOT_UNRESOLVED",
        research_question_statement=(
            "Should a supported component be treated as unresolved?"
        ),
    )
    with pytest.raises(AgentLoopError, match="UNKNOWN or MIXED"):
        runtime.record_postmortem(
            forged,
            at="2026-09-19T13:05:05Z",
        )

    assert runtime.snapshot().phase is AgentLoopPhase.REFLECT
    assert runtime.snapshot().postmortem_id is None

def test_admissible_direct_action_cannot_predate_observation_availability(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)

    backdated = Action(
        environment_id=environment.environment_id,
        observation_id=observation.observation_id,
        action_type="WAIT",
        decided_at="2026-09-19T13:00:00Z",
    )
    with pytest.raises(
        AgentLoopError,
        match="predates observation availability",
    ):
        runtime.commit_action(
            backdated,
            episode=environment.episode,
            observation=observation,
            effect_state=ExternalEffectState.NONE,
            at="2026-09-19T13:00:05Z",
        )

    snapshot = runtime.snapshot()
    assert snapshot.phase is AgentLoopPhase.ACT_OR_ABSTAIN
    assert snapshot.action_id is None



def test_schema_v3_restart_rejects_rehashed_action_payload_relabelling_before_checkpoint(
    tmp_path,
):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
        parameters=(("candidate_id", "candidate-1"),),
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    original = json_load(runtime.path)
    assert original["decisions"][0]["parameters"] == [
        ["candidate_id", "candidate-1"]
    ]

    relabelled = json.loads(json.dumps(original))
    relabelled["decisions"][0]["action_type"] = "PAPER_PROPOSAL"
    rewrite_with_valid_state_digest(runtime.path, relabelled)
    with pytest.raises(AgentLoopError, match="decision action identity mismatch"):
        AgentLoopRuntime(runtime.path)

    reparameterized = json.loads(json.dumps(original))
    parameters = [["candidate_id", "candidate-2"]]
    reparameterized["decisions"][0]["parameters"] = parameters
    reparameterized["decisions"][0]["parameters_sha256"] = canonical_sha256(parameters)
    reparameterized["decisions"][0]["decision_payload_id"] = canonical_sha256(
        {
            "action_type": reparameterized["decisions"][0]["action_type"],
            "parameters": parameters,
        }
    )
    rewrite_with_valid_state_digest(runtime.path, reparameterized)
    with pytest.raises(AgentLoopError, match="decision action identity mismatch"):
        AgentLoopRuntime(runtime.path)


def test_schema_v3_checkpoint_cannot_downgrade_or_rebind_action_payload_evidence(
    tmp_path,
):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
        parameters=(("candidate_id", "candidate-1"),),
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    outcome, reward, transition = _resolve(environment, action)
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )
    attribution = _attribution(environment, transition, outcome, reward)
    runtime.record_attribution(
        attribution,
        at="2026-09-19T13:05:04Z",
    )
    postmortem = ReflectionPostmortem(
        attribution_id=attribution.attribution_id,
        transition_id=transition.transition_id,
        created_at="2026-09-19T13:05:05Z",
        unresolved_components=(AttributionComponent.RANDOMNESS,),
        summary_code="V3_ACTION_PAYLOAD_BINDING",
    )
    runtime.record_postmortem(
        postmortem,
        at="2026-09-19T13:05:05Z",
    )
    runtime.commit_checkpoint(
        environment.checkpoint(),
        at="2026-09-19T13:05:06Z",
    )
    original = json_load(runtime.path)

    downgraded = json.loads(json.dumps(original))
    downgraded["decisions"][0]["parameters"] = None
    rewrite_with_valid_state_digest(runtime.path, downgraded)
    with pytest.raises(
        AgentLoopError,
        match="missing outside legacy migration baseline",
    ):
        AgentLoopRuntime(runtime.path)

    relabelled = json.loads(json.dumps(original))
    relabelled["decisions"][0]["action_type"] = "PAPER_PROPOSAL"
    rewrite_with_valid_state_digest(runtime.path, relabelled)
    with pytest.raises(AgentLoopError, match="decision action identity mismatch"):
        AgentLoopRuntime(runtime.path)

    reparameterized = json.loads(json.dumps(original))
    parameters = [["candidate_id", "candidate-2"]]
    reparameterized["decisions"][0]["parameters"] = parameters
    reparameterized["decisions"][0]["parameters_sha256"] = canonical_sha256(parameters)
    reparameterized["decisions"][0]["decision_payload_id"] = canonical_sha256(
        {
            "action_type": reparameterized["decisions"][0]["action_type"],
            "parameters": parameters,
        }
    )
    rewrite_with_valid_state_digest(runtime.path, reparameterized)
    with pytest.raises(AgentLoopError, match="decision action identity mismatch"):
        AgentLoopRuntime(runtime.path)


def test_restart_rejects_self_consistent_cross_linked_agent_memory(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    _advance_to_action(runtime)
    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2026-09-19T13:00:05Z",
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2026-09-19T13:00:05Z",
    )
    outcome, reward, transition = _resolve(environment, action)
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2026-09-19T13:05:02Z",
    )
    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2026-09-19T13:05:03Z",
    )
    attribution = _attribution(environment, transition, outcome, reward)
    runtime.record_attribution(
        attribution,
        at="2026-09-19T13:05:04Z",
    )
    postmortem = ReflectionPostmortem(
        attribution_id=attribution.attribution_id,
        transition_id=transition.transition_id,
        created_at="2026-09-19T13:05:05Z",
        unresolved_components=(AttributionComponent.RANDOMNESS,),
        summary_code="UNRESOLVED_RANDOMNESS_REQUIRES_RESEARCH",
        research_question_statement="Test the unresolved causal component.",
    )
    runtime.record_postmortem(
        postmortem,
        at="2026-09-19T13:05:05Z",
    )
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json",
        registry,
    )
    runtime.handoff_research(
        supervisor,
        budget_units=3,
        at="2026-09-19T13:05:06Z",
    )
    original = json_load(runtime.path)

    def clone():
        return json.loads(json.dumps(original))

    corruptions = []

    malformed_decision = clone()
    malformed_decision["decisions"][0]["untrusted_field"] = "accepted-before-fix"
    corruptions.append((malformed_decision, "decision record fields mismatch"))

    orphan_current = clone()
    orphan_current["current"]["action_id"] = "a" * 64
    corruptions.append((orphan_current, "current action does not bind"))

    orphan_resolution = clone()
    orphan_resolution["resolutions"][0]["action_id"] = "b" * 64
    corruptions.append((orphan_resolution, "resolution does not bind"))

    cross_linked_attribution = clone()
    cross_linked_attribution["attributions"][0]["transition_id"] = "c" * 64
    corruptions.append((cross_linked_attribution, "attribution record is not canonical"))

    orphan_handoff = clone()
    orphan_handoff["research_handoffs"][0]["postmortem_id"] = "d" * 64
    corruptions.append((orphan_handoff, "research handoff does not bind"))

    for corrupted, expected_error in corruptions:
        rewrite_with_valid_state_digest(runtime.path, corrupted)
        with pytest.raises(AgentLoopError, match=expected_error):
            AgentLoopRuntime(runtime.path)

    rewrite_with_valid_state_digest(runtime.path, original)
    assert AgentLoopRuntime(runtime.path).snapshot().research_run_id is not None
