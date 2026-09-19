from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.agent_loop import (
    AgentLoopPhase,
    AgentLoopRuntime,
    AttributionComponent,
    AttributionFinding,
    AttributionStatus,
    ExternalEffectState,
    OutcomeAttribution,
    ReflectionPostmortem,
)
from autosport.closed_loop_learning import (
    ClosedLoopBindingError,
    bind_challenger_artifact,
)
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.research_curriculum import (
    CurriculumPurpose,
    NightResearchCurriculum,
    ReplayCandidate,
    ReplayEvidenceBinding,
    ReplayProvenance,
)
from autosport.research_supervisor import ResearchPhase, ResearchSupervisor
from autosport.research_supervisor_actions import (
    checkpoint_causal_environment,
    commit_memory_and_next_question,
    finalize_factory_decision,
    stage_factory_evaluation,
)
from autosport.research_trigger_adapter import ResearchTriggerAdapter
from autosport.scientific_registry import (
    PromotionAction,
    ResearchQuestion,
    ScientificRegistry,
)

from closed_loop_factory_fixture import (
    CONFIG_SHA,
    SOURCE_SHA,
    T0,
    T7,
    T8,
    build_closed_loop_factory,
)

GOAL_SHA = "d" * 64
RISK_SHA = "e" * 64
RANDOMNESS_SHA = "1" * 64


def _advance_to(supervisor, run_id, target):
    while supervisor.status(run_id).phase is not target:
        current = supervisor.status(run_id).phase
        supervisor.advance(run_id, expected_phase=current, at=T7)


def _resolved_agent_episode(tmp_path):
    identity = EnvironmentIdentity(
        source_id="autosport.replay-provenance/PAPER_LIVE/closed-loop-fixture",
        config_id="closed-loop-config",
        data_id="closed-loop-observation-data",
        protocol_id="closed-loop-agent-protocol",
        cutoff_ts="2026-01-10T00:00:00+00:00",
        seed=17,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="closed-loop-episode",
        policy_id="policy-v1",
        admissible_actions=frozenset({"WAIT"}),
    )
    runtime = AgentLoopRuntime.initialize_pristine(
        tmp_path / "agent-loop.json",
        loop_id="closed-loop-agent",
        environment_checkpoint=environment.checkpoint(),
        policy_id=environment.episode.policy_id,
        economic_goal_fingerprint=GOAL_SHA,
        risk_fingerprint=RISK_SHA,
        source_sha256=SOURCE_SHA,
        config_sha256=CONFIG_SHA,
        at="2025-12-30T00:00:00+00:00",
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2025-12-30T00:00:00+00:00",
        available_at="2025-12-30T00:00:01+00:00",
        evidence=(("market_state", "causal-snapshot"),),
    )
    runtime.begin_observation(
        observation,
        environment_identity=identity,
        at="2025-12-30T00:00:01+00:00",
    )
    for phase, at in (
        (AgentLoopPhase.OBSERVE, "2025-12-30T00:00:02+00:00"),
        (AgentLoopPhase.ASSESS, "2025-12-30T00:00:03+00:00"),
        (AgentLoopPhase.PLAN, "2025-12-30T00:00:04+00:00"),
        (AgentLoopPhase.DECIDE, "2025-12-30T00:00:05+00:00"),
    ):
        runtime.advance(expected=phase, at=at)

    action = environment.act(
        observation,
        action_type="WAIT",
        decision_at="2025-12-30T00:00:05+00:00",
    )
    first_receipt = runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2025-12-30T00:00:05+00:00",
    )
    assert first_receipt.newly_committed is True
    retry = AgentLoopRuntime(runtime.path).commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.NONE,
        at="2025-12-30T00:00:06+00:00",
    )
    assert retry.newly_committed is False
    assert retry.may_execute is False

    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at="2025-12-31T00:00:00+00:00",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("paper_result", "resolved"),),
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("-0.25"),
        available_at="2025-12-31T00:00:01+00:00",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("reward_rule", "paper-economic-v1"),),
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at="2025-12-31T00:00:02+00:00",
    )
    runtime.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at="2025-12-31T00:00:02+00:00",
    )
    runtime.advance(
        expected=AgentLoopPhase.EVALUATE,
        at="2025-12-31T00:00:03+00:00",
    )
    attribution = OutcomeAttribution(
        environment_id=environment.environment_id,
        episode_id=environment.episode.episode_id,
        transition_id=transition.transition_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward_id=reward.reward_id,
        reward_value=reward.reward,
        truth=reward.truth,
        simulation_model_id=None,
        attributed_at="2025-12-31T00:00:04+00:00",
        findings=(
            AttributionFinding(
                component=AttributionComponent.RANDOMNESS,
                status=AttributionStatus.UNKNOWN,
                evidence_sha256=RANDOMNESS_SHA,
                evidence_available_at="2025-12-31T00:00:03+00:00",
                reason_code="UNRESOLVED_RANDOMNESS",
            ),
        ),
    )
    runtime.record_attribution(
        attribution,
        at="2025-12-31T00:00:04+00:00",
    )
    duplicate = runtime.record_attribution(
        attribution,
        at="2025-12-31T00:00:05+00:00",
    )
    assert duplicate.attribution_id == attribution.attribution_id

    runtime.record_postmortem(
        ReflectionPostmortem(
            attribution_id=attribution.attribution_id,
            transition_id=transition.transition_id,
            created_at="2025-12-31T00:00:05+00:00",
            unresolved_components=(AttributionComponent.RANDOMNESS,),
            summary_code="RESEARCH_REQUIRED",
            research_question_statement=(
                "Does the bounded challenger improve frozen causal evidence without "
                "widening owner risk authority?"
            ),
        ),
        at="2025-12-31T00:00:05+00:00",
    )
    return identity, environment, runtime, outcome, reward, transition


def test_closed_loop_research_factory_restart_and_next_decision(tmp_path):
    identity, environment, runtime, outcome, reward, transition = (
        _resolved_agent_episode(tmp_path)
    )
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json",
        registry,
    )

    origin = runtime.handoff_research(
        supervisor,
        budget_units=64,
        deadline_at="2026-01-10T00:00:00+00:00",
        at="2025-12-31T00:00:06+00:00",
    )
    checkpoint = environment.checkpoint()
    checkpointed = runtime.commit_checkpoint(
        checkpoint,
        at="2025-12-31T00:00:07+00:00",
    )
    assert checkpointed.economic_goal_fingerprint == GOAL_SHA
    assert checkpointed.risk_fingerprint == RISK_SHA

    replay_binding = ReplayEvidenceBinding(
        identity=identity,
        episode=environment.episode,
        checkpoint=checkpoint,
        transition=transition,
        outcome=outcome,
        reward=reward,
    )
    candidate = ReplayCandidate(
        question_id=origin.question_id,
        evidence_binding=replay_binding,
        available_at="2025-12-31T00:00:07+00:00",
        reasons=("closed-loop-postmortem",),
        selector_features=(("unresolved_component", "RANDOMNESS"),),
        priority=10,
        expected_learning_value=Decimal("0.8"),
    )
    curriculum = NightResearchCurriculum.initialize_pristine(
        tmp_path / "research-curriculum.json",
        ResearchTriggerAdapter(supervisor),
        max_budget_units=128,
    )
    selection = curriculum.select(
        (candidate,),
        purpose=CurriculumPurpose.CURRICULUM,
        selector_policy_version="closed-loop-v1",
        as_of=T0,
        seed=17,
        budget_units=64,
    )
    assert len(supervisor.list_runs()) == 1
    assert selection.selected_question_id == runtime.snapshot().research_question_id
    assert selection.selected_provenance is ReplayProvenance.PAPER_LIVE
    assert selection.selected_evidence_truth is EvidenceTruth.OBSERVED

    question_entry = registry.get(
        "ResearchQuestion",
        selection.selected_question_id,
    )
    assert question_entry is not None
    question = ResearchQuestion(**question_entry.payload)
    runner, rule, spec, points = build_closed_loop_factory(
        tmp_path,
        registry,
        question,
        environment.environment_id,
    )

    _advance_to(supervisor, origin.run_id, ResearchPhase.EXPERIMENT)
    _, staged = stage_factory_evaluation(
        supervisor,
        origin.run_id,
        runner=runner,
        spec=spec,
        points=points,
        rule=rule,
        at=T7,
    )
    artifact = bind_challenger_artifact(
        runtime=AgentLoopRuntime(runtime.path),
        curriculum=curriculum,
        selection=selection,
        replay_binding=replay_binding,
        supervisor=supervisor,
        registry=registry,
        spec=spec,
        staged=staged,
    )
    assert artifact.research_question_id == question.question_id
    assert artifact.environment_id == environment.environment_id
    assert artifact.economic_goal_fingerprint == GOAL_SHA
    assert artifact.risk_fingerprint == RISK_SHA
    assert artifact.replay_provenance is ReplayProvenance.PAPER_LIVE
    assert len(artifact.artifact_id) == 64

    relabelled = replace(
        selection,
        selected_provenance=ReplayProvenance.HISTORICAL_OBSERVED,
    )
    with pytest.raises(
        ClosedLoopBindingError,
        match="selection is not exact durable curriculum evidence",
    ):
        bind_challenger_artifact(
            runtime=runtime,
            curriculum=curriculum,
            selection=relabelled,
            dispatch=dispatch,
            supervisor=supervisor,
            registry=registry,
            spec=spec,
            staged=staged,
        )

    stale_stage = replace(
        staged,
        strategy_version_id="strategy-closed-loop-stale",
    )
    with pytest.raises(
        ClosedLoopBindingError,
        match="staged factory identity mismatch",
    ):
        bind_challenger_artifact(
            runtime=runtime,
            curriculum=curriculum,
            selection=selection,
            dispatch=dispatch,
            supervisor=supervisor,
            registry=registry,
            spec=spec,
            staged=stale_stage,
        )

    _advance_to(
        supervisor,
        origin.run_id,
        ResearchPhase.FORWARD_PAPER_SHADOW,
    )
    checkpoint_causal_environment(
        supervisor,
        origin.run_id,
        environment=environment,
        at=T7,
    )
    postmortem_snapshot, decision = finalize_factory_decision(
        supervisor,
        origin.run_id,
        runner=runner,
        spec=spec,
        staged=staged,
        final_action=staged.proposed_action,
        decided_at=T7,
        robustness_evidence_sha256=artifact.artifact_id,
        forward_evidence_sha256=checkpoint.checkpoint_id,
        reason="closed-loop evidence remains within frozen owner authority",
    )
    assert postmortem_snapshot.phase is ResearchPhase.POSTMORTEM
    decision_entry = registry.get(
        "PromotionDecision",
        decision.promotion_decision_id,
    )
    assert decision_entry is not None
    evidence_id = decision_entry.payload["promotion_evidence_id"]
    assert evidence_id is not None
    promotion_evidence = registry.get("PromotionEvidence", evidence_id)
    assert promotion_evidence is not None
    assert (
        promotion_evidence.payload["research_question_id"]
        == question.question_id
    )
    assert promotion_evidence.payload["holdout_access_id"]

    next_question = ResearchQuestion(
        "question-closed-loop-next",
        "What should the next causally independent paper episode test?",
        SOURCE_SHA,
        T7,
    )
    complete = commit_memory_and_next_question(
        supervisor,
        origin.run_id,
        decision=decision,
        staged=staged,
        next_question=next_question,
        at=T7,
    )
    assert complete.phase is ResearchPhase.COMPLETE

    reopened_registry = ScientificRegistry(registry.path)
    reopened_supervisor = ResearchSupervisor(
        supervisor.path,
        reopened_registry,
    )
    reopened_curriculum = NightResearchCurriculum(
        curriculum.path,
        ResearchTriggerAdapter(reopened_supervisor),
        max_budget_units=128,
    )
    reopened_runtime = AgentLoopRuntime(runtime.path)
    resumed_environment = CausalLearningEnvironment.resume(
        identity,
        episode_key=environment.episode.episode_key,
        policy_id=environment.episode.policy_id,
        admissible_actions=frozenset(environment.episode.admissible_actions),
        checkpoint=checkpoint,
    )
    assert reopened_supervisor.status(origin.run_id).phase is ResearchPhase.COMPLETE
    assert len(reopened_supervisor.list_runs()) == 1
    assert reopened_curriculum.snapshot()["dispatches"] == {}
    assert reopened_runtime.snapshot().economic_goal_fingerprint == GOAL_SHA
    assert reopened_runtime.snapshot().risk_fingerprint == RISK_SHA

    next_observation = Observation(
        environment_id=identity.environment_id,
        observed_at=T8,
        available_at=T8,
        evidence=(("market_state", "next-causal-snapshot"),),
    )
    reopened_runtime.begin_observation(
        next_observation,
        environment_identity=identity,
        at=T8,
    )
    for phase in (
        AgentLoopPhase.OBSERVE,
        AgentLoopPhase.ASSESS,
        AgentLoopPhase.PLAN,
        AgentLoopPhase.DECIDE,
    ):
        reopened_runtime.advance(expected=phase, at=T8)
    next_action = resumed_environment.act(
        next_observation,
        action_type="WAIT",
        decision_at=T8,
    )
    receipt = reopened_runtime.commit_action(
        next_action,
        episode=resumed_environment.episode,
        observation=next_observation,
        effect_state=ExternalEffectState.NONE,
        at=T8,
    )
    assert receipt.newly_committed is True
    final = reopened_runtime.snapshot()
    assert final.phase is AgentLoopPhase.WAIT_OUTCOME
    assert final.economic_goal_fingerprint == GOAL_SHA
    assert final.risk_fingerprint == RISK_SHA
