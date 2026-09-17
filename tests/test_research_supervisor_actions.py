from decimal import Decimal

import pytest

from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.research_factory_bridge import finalize_staged_candidate
from autosport.research_supervisor import (
    ResearchPhase,
    ResearchSupervisor,
    ResearchSupervisorError,
    ResearchTrigger,
)
from autosport.research_supervisor_actions import (
    checkpoint_causal_environment,
    commit_memory_and_next_question,
    finalize_factory_decision,
    stage_factory_evaluation,
)
from autosport.scientific_registry import ResearchQuestion
from autosport.strategy_model_factory import ExperimentRunner
from test_strategy_model_factory import (
    SHA_A,
    SHA_B,
    T4,
    T5,
    T6,
    T7,
    _candidate_points,
    _candidate_spec,
    _factory_foundation,
)


def _supervisor_for_factory(tmp_path, registry):
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json", registry
    )
    started = supervisor.accept_trigger(
        ResearchTrigger(
            trigger_id="continuous-research-1",
            question_id="question-factory",
            requested_at=T4,
            budget_units=64,
            deadline_at="2026-01-10T00:00:00+00:00",
        )
    )
    return supervisor, started.run_id


def _advance_to(supervisor, run_id, target, *, at=T7):
    while supervisor.status(run_id).phase is not target:
        current = supervisor.status(run_id).phase
        supervisor.advance(run_id, expected_phase=current, at=at)


def _resolved_environment():
    identity = EnvironmentIdentity(
        source_id="paper-replay-source-v1",
        config_id="learning-config-v1",
        data_id="dataset-factory",
        protocol_id="protocol-factory",
        cutoff_ts=T7,
        seed=17,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="paper-shadow-1",
        policy_id="policy-transparent-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL"}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at=T5,
        available_at=T5,
        evidence=(("candidate", "strategy-v2"),),
    )
    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at=T5,
        parameters=(("candidate_id", "strategy-v2"),),
    )
    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at=T6,
        truth=EvidenceTruth.OBSERVED,
        evidence=(("paper_result", "resolved"),),
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at=T6,
        truth=EvidenceTruth.OBSERVED,
        evidence=(("reward_rule", "paper-return-v1"),),
    )
    environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at=T6,
    )
    return environment


def test_stage_factory_wrong_phase_has_no_scientific_registry_side_effect(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    runner = ExperimentRunner(registry, store)
    supervisor, run_id = _supervisor_for_factory(tmp_path, registry)
    before = registry.path.read_bytes()

    with pytest.raises(ResearchSupervisorError, match="not admitted for authority write"):
        stage_factory_evaluation(
            supervisor,
            run_id,
            runner=runner,
            spec=_candidate_spec(),
            points=_candidate_points(),
            rule=rule,
            at=T7,
        )

    assert registry.path.read_bytes() == before
    assert supervisor.status(run_id).phase is not ResearchPhase.EXPERIMENT


def test_finalize_factory_wrong_phase_has_no_promotion_or_postmortem_side_effect(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    runner = ExperimentRunner(registry, store)
    supervisor, run_id = _supervisor_for_factory(tmp_path, registry)
    spec = _candidate_spec()

    _advance_to(supervisor, run_id, ResearchPhase.EXPERIMENT)
    _, staged = stage_factory_evaluation(
        supervisor,
        run_id,
        runner=runner,
        spec=spec,
        points=_candidate_points(),
        rule=rule,
        at=T7,
    )
    _advance_to(supervisor, run_id, ResearchPhase.FORWARD_PAPER_SHADOW)
    before = registry.path.read_bytes()
    assert registry.get("PromotionDecision", staged.promotion_decision_id) is None

    with pytest.raises(ResearchSupervisorError, match="not admitted for authority write"):
        finalize_factory_decision(
            supervisor,
            run_id,
            runner=runner,
            spec=spec,
            staged=staged,
            final_action=staged.proposed_action,
            decided_at=T7,
            robustness_evidence_sha256=SHA_A,
            forward_evidence_sha256=SHA_B,
            reason="wrong-phase call must not publish durable science",
        )

    assert registry.path.read_bytes() == before
    assert registry.get("PromotionDecision", staged.promotion_decision_id) is None
    assert supervisor.status(run_id).phase is ResearchPhase.FORWARD_PAPER_SHADOW


def test_continuous_supervisor_consumes_real_factory_environment_memory_and_restart(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    runner = ExperimentRunner(registry, store)
    supervisor, run_id = _supervisor_for_factory(tmp_path, registry)
    spec = _candidate_spec()

    _advance_to(supervisor, run_id, ResearchPhase.EXPERIMENT)
    staged_snapshot, staged = stage_factory_evaluation(
        supervisor,
        run_id,
        runner=runner,
        spec=spec,
        points=_candidate_points(),
        rule=rule,
        at=T7,
    )
    assert staged_snapshot.phase is ResearchPhase.CAUSAL_EVALUATION
    assert registry.get("Experiment", staged.experiment_id) is not None
    assert registry.get("EvaluationBundle", staged.evaluation_bundle_id) is not None
    assert registry.get("PromotionDecision", staged.promotion_decision_id) is None

    replay_snapshot, replay_staged = stage_factory_evaluation(
        supervisor,
        run_id,
        runner=runner,
        spec=spec,
        points=_candidate_points(),
        rule=rule,
        at=T7,
    )
    assert replay_snapshot == staged_snapshot
    assert replay_staged == staged

    _advance_to(supervisor, run_id, ResearchPhase.FORWARD_PAPER_SHADOW)
    environment = _resolved_environment()
    decision_snapshot, evidence = checkpoint_causal_environment(
        supervisor, run_id, environment=environment, at=T7
    )
    assert decision_snapshot.phase is ResearchPhase.DECISION
    assert evidence.checkpoint.step_index == 1
    replay_decision, replay_evidence = checkpoint_causal_environment(
        supervisor, run_id, environment=environment, at=T7
    )
    assert replay_decision == decision_snapshot
    assert replay_evidence == evidence

    # Required crash boundary: canonical decision is durable while the supervisor
    # checkpoint is still DECISION. Exact wrapper redelivery must converge once.
    durable_before_checkpoint = finalize_staged_candidate(
        runner,
        spec,
        staged,
        final_action=staged.proposed_action,
        decided_at=T7,
        robustness_evidence_sha256=SHA_A,
        forward_evidence_sha256=SHA_B,
        reason="resolved causal evidence supports the canonical factory decision",
        retest_conditions=(),
    )
    assert registry.get(
        "PromotionDecision", durable_before_checkpoint.promotion_decision_id
    ) is not None
    assert supervisor.status(run_id).phase is ResearchPhase.DECISION

    postmortem_snapshot, decision = finalize_factory_decision(
        supervisor,
        run_id,
        runner=runner,
        spec=spec,
        staged=staged,
        final_action=staged.proposed_action,
        decided_at=T7,
        robustness_evidence_sha256=SHA_A,
        forward_evidence_sha256=SHA_B,
        reason="resolved causal evidence supports the canonical factory decision",
        retest_conditions=(),
    )
    assert decision == durable_before_checkpoint
    assert postmortem_snapshot.phase is ResearchPhase.POSTMORTEM

    next_question = ResearchQuestion(
        question_id="question-factory-next",
        statement="Which bounded challenger should test the next causal uncertainty?",
        source_sha256=SHA_A,
        created_at=T7,
    )
    completed = commit_memory_and_next_question(
        supervisor,
        run_id,
        decision=decision,
        staged=staged,
        next_question=next_question,
        at=T7,
    )
    assert completed.phase is ResearchPhase.COMPLETE
    assert registry.get("ResearchQuestion", next_question.question_id) is not None
    assert dict(completed.bindings)["next_question_id"] == next_question.question_id

    reopened = ResearchSupervisor(supervisor.path, registry)
    replay_complete = commit_memory_and_next_question(
        reopened,
        run_id,
        decision=decision,
        staged=staged,
        next_question=next_question,
        at=T7,
    )
    assert replay_complete == completed


def test_empty_learning_environment_cannot_unlock_decision(tmp_path):
    registry, _, _, _, _, _ = _factory_foundation(tmp_path)
    supervisor, run_id = _supervisor_for_factory(tmp_path, registry)
    _advance_to(supervisor, run_id, ResearchPhase.FORWARD_PAPER_SHADOW)
    identity = EnvironmentIdentity(
        source_id="paper-replay-source-v1",
        config_id="learning-config-v1",
        data_id="dataset-factory",
        protocol_id="protocol-factory",
        cutoff_ts=T7,
        seed=17,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="empty-paper-shadow",
        policy_id="policy-transparent-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL"}),
    )

    with pytest.raises(ResearchSupervisorError, match="at least one resolved transition"):
        checkpoint_causal_environment(supervisor, run_id, environment=environment, at=T7)
    assert supervisor.status(run_id).phase is ResearchPhase.FORWARD_PAPER_SHADOW
