import pytest

from autosport.research_supervisor import ResearchPhase, ResearchSupervisorError
from autosport.research_supervisor_actions import (
    checkpoint_causal_environment,
    commit_memory_and_next_question,
    finalize_factory_decision,
    stage_factory_evaluation,
)
from autosport.scientific_registry import ResearchQuestion
from autosport.strategy_model_factory import ExperimentRunner
from test_research_supervisor_actions import (
    _advance_to,
    _resolved_environment,
    _supervisor_for_factory,
)
from test_strategy_model_factory import (
    SHA_A,
    SHA_B,
    T7,
    _candidate_points,
    _candidate_spec,
    _factory_foundation,
)


def test_complete_redelivery_rejects_changed_next_question_before_registry_write(tmp_path):
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
    checkpoint_causal_environment(
        supervisor,
        run_id,
        environment=_resolved_environment(),
        at=T7,
    )
    _, decision = finalize_factory_decision(
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

    canonical_question = ResearchQuestion(
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
        next_question=canonical_question,
        at=T7,
    )
    assert completed.phase is ResearchPhase.COMPLETE

    changed_question = ResearchQuestion(
        question_id="question-factory-unexpected",
        statement="This changed replay must not enter durable research memory.",
        source_sha256=SHA_B,
        created_at=T7,
    )
    before = registry.path.read_bytes()

    with pytest.raises(
        ResearchSupervisorError,
        match="conflicting redelivery for durable binding next_question_id",
    ):
        commit_memory_and_next_question(
            supervisor,
            run_id,
            decision=decision,
            staged=staged,
            next_question=changed_question,
            at=T7,
        )

    assert registry.path.read_bytes() == before
    assert registry.get("ResearchQuestion", changed_question.question_id) is None
    assert registry.get("ResearchQuestion", canonical_question.question_id) is not None
