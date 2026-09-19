import json
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


def test_full_paper_loop_attribution_research_handoff_and_restart(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)

    runtime.begin_observation(
        observation,
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


def test_restart_never_reauthorizes_same_action(tmp_path):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
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


def test_future_evidence_and_observed_simulated_relabel_fail_closed(
    tmp_path,
):
    environment = _environment()
    runtime = _runtime(tmp_path, environment)
    observation = _observation(environment)
    runtime.begin_observation(
        observation,
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

