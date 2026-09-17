"""Stateless causal bridges for the canonical continuous ResearchSupervisor.

The functions here do not own scheduling, scientific memory, factory evaluation, or
learning-environment state. They only make critical supervisor phase transitions
consume the already-canonical durable authorities, while remaining safe to redeliver
after a crash between the authority write and the supervisor checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .learning_environment import CausalLearningEnvironment, EnvironmentCheckpoint
from .research_factory_bridge import (
    FinalizedFactoryDecision,
    StagedFactoryEvaluation,
    finalize_staged_candidate,
    stage_baseline_candidate,
)
from .research_supervisor import (
    ResearchPhase,
    ResearchSupervisor,
    ResearchSupervisorError,
    SupervisorSnapshot,
)
from .scientific_registry import PromotionAction, ResearchQuestion
from .strategy_model_factory import (
    ExperimentRunner,
    FactoryCandidateSpec,
    PromotionRule,
    TrainingPoint,
)


_PHASES = tuple(ResearchPhase)
_PHASE_INDEX = {phase: index for index, phase in enumerate(_PHASES)}


@dataclass(frozen=True, slots=True)
class EnvironmentPhaseEvidence:
    """Exact non-empty causal-environment checkpoint consumed by the supervisor."""

    environment_id: str
    checkpoint: EnvironmentCheckpoint


def _binding_map(snapshot: SupervisorSnapshot) -> dict[str, str]:
    return dict(snapshot.bindings)


def _require_replayed_bindings(
    snapshot: SupervisorSnapshot,
    *,
    required: tuple[tuple[str, str], ...],
    at_least_phase: ResearchPhase,
) -> SupervisorSnapshot:
    if _PHASE_INDEX[snapshot.phase] < _PHASE_INDEX[at_least_phase]:
        raise ResearchSupervisorError(
            f"supervisor phase {snapshot.phase.value} precedes replay boundary "
            f"{at_least_phase.value}"
        )
    bound = _binding_map(snapshot)
    for key, value in required:
        existing = bound.get(key)
        if existing != value:
            raise ResearchSupervisorError(
                f"conflicting redelivery for durable binding {key}: "
                f"expected {value!r}, found {existing!r}"
            )
    return snapshot


def _advance_once_or_replay(
    supervisor: ResearchSupervisor,
    run_id: str,
    *,
    expected_phase: ResearchPhase,
    next_phase: ResearchPhase,
    at: str,
    budget_cost: int,
    bindings: tuple[tuple[str, str], ...],
) -> SupervisorSnapshot:
    snapshot = supervisor.status(run_id)
    if snapshot.phase is expected_phase:
        return supervisor.advance(
            run_id,
            expected_phase=expected_phase,
            at=at,
            budget_cost=budget_cost,
            bindings=bindings,
        )
    return _require_replayed_bindings(
        snapshot,
        required=bindings,
        at_least_phase=next_phase,
    )


def stage_factory_evaluation(
    supervisor: ResearchSupervisor,
    run_id: str,
    *,
    runner: ExperimentRunner,
    spec: FactoryCandidateSpec,
    points: Sequence[TrainingPoint],
    rule: PromotionRule,
    at: str,
    budget_cost: int = 1,
    minimum_train_size: int | None = None,
) -> tuple[SupervisorSnapshot, StagedFactoryEvaluation]:
    """Run/publish the canonical factory stage, then checkpoint EXPERIMENT once.

    The canonical research-factory bridge publishes model/strategy/experiment and
    evaluation evidence without publishing a PromotionDecision. Exact redelivery
    reconstructs that staged evidence and this function then reuses the already
    committed supervisor bindings instead of creating another experiment.
    """

    if not isinstance(supervisor, ResearchSupervisor):
        raise TypeError("supervisor must be ResearchSupervisor")
    staged = stage_baseline_candidate(
        runner,
        spec,
        points,
        rule=rule,
        minimum_train_size=minimum_train_size,
    )
    bindings = (
        ("evaluation_bundle_id", staged.evaluation_bundle_id),
        ("experiment_id", staged.experiment_id),
        ("model_version_id", staged.model_version_id),
        ("strategy_version_id", staged.strategy_version_id),
    )
    snapshot = _advance_once_or_replay(
        supervisor,
        run_id,
        expected_phase=ResearchPhase.EXPERIMENT,
        next_phase=ResearchPhase.CAUSAL_EVALUATION,
        at=at,
        budget_cost=budget_cost,
        bindings=bindings,
    )
    return snapshot, staged


def checkpoint_causal_environment(
    supervisor: ResearchSupervisor,
    run_id: str,
    *,
    environment: CausalLearningEnvironment,
    at: str,
    budget_cost: int = 1,
) -> tuple[SupervisorSnapshot, EnvironmentPhaseEvidence]:
    """Bind resolved causal-learning evidence to FORWARD/PAPER -> DECISION.

    ``environment.checkpoint()`` itself fails closed while an action is unresolved.
    An empty environment is not evidence and cannot unlock DECISION. If a crash
    happens after the supervisor checkpoint, exact redelivery returns the already-
    advanced snapshot; a different environment/checkpoint fails closed.
    """

    if not isinstance(supervisor, ResearchSupervisor):
        raise TypeError("supervisor must be ResearchSupervisor")
    if not isinstance(environment, CausalLearningEnvironment):
        raise TypeError("environment must be CausalLearningEnvironment")
    checkpoint = environment.checkpoint()
    if checkpoint.step_index <= 0:
        raise ResearchSupervisorError(
            "causal learning environment must contain at least one resolved transition"
        )
    evidence = EnvironmentPhaseEvidence(
        environment_id=environment.environment_id,
        checkpoint=checkpoint,
    )
    bindings = (
        ("environment_checkpoint_id", checkpoint.checkpoint_id),
        ("environment_id", environment.environment_id),
    )
    snapshot = _advance_once_or_replay(
        supervisor,
        run_id,
        expected_phase=ResearchPhase.FORWARD_PAPER_SHADOW,
        next_phase=ResearchPhase.DECISION,
        at=at,
        budget_cost=budget_cost,
        bindings=bindings,
    )
    return snapshot, evidence


def finalize_factory_decision(
    supervisor: ResearchSupervisor,
    run_id: str,
    *,
    runner: ExperimentRunner,
    spec: FactoryCandidateSpec,
    staged: StagedFactoryEvaluation,
    final_action: PromotionAction,
    decided_at: str,
    robustness_evidence_sha256: str,
    forward_evidence_sha256: str,
    reason: str,
    budget_cost: int = 1,
    retest_conditions: Sequence[str] = (),
) -> tuple[SupervisorSnapshot, FinalizedFactoryDecision]:
    """Publish the canonical factory decision, then checkpoint DECISION -> POSTMORTEM.

    The ordering is deliberate. A crash after ``finalize_staged_candidate`` has
    durably published PromotionDecision/Postmortem but before ``advance`` leaves the
    supervisor in DECISION. Exact retry replays the factory's immutable write and
    then advances the supervisor once. Any changed decision payload conflicts in the
    ScientificRegistry, while any changed supervisor binding conflicts here.
    """

    if not isinstance(supervisor, ResearchSupervisor):
        raise TypeError("supervisor must be ResearchSupervisor")
    result = finalize_staged_candidate(
        runner,
        spec=spec,
        staged=staged,
        final_action=final_action,
        decided_at=decided_at,
        robustness_evidence_sha256=robustness_evidence_sha256,
        forward_evidence_sha256=forward_evidence_sha256,
        reason=reason,
        retest_conditions=tuple(retest_conditions),
    )
    bindings = (
        ("evaluation_bundle_id", staged.evaluation_bundle_id),
        ("experiment_id", staged.experiment_id),
        ("promotion_decision_id", result.promotion_decision_id),
        ("strategy_version_id", staged.strategy_version_id),
        ("model_version_id", staged.model_version_id),
    )
    snapshot = _advance_once_or_replay(
        supervisor,
        run_id,
        expected_phase=ResearchPhase.DECISION,
        next_phase=ResearchPhase.POSTMORTEM,
        at=decided_at,
        budget_cost=budget_cost,
        bindings=bindings,
    )
    return snapshot, result


def commit_memory_and_next_question(
    supervisor: ResearchSupervisor,
    run_id: str,
    *,
    decision: FinalizedFactoryDecision,
    staged: StagedFactoryEvaluation,
    next_question: ResearchQuestion,
    at: str,
    postmortem_budget_cost: int = 1,
    memory_budget_cost: int = 1,
    next_question_budget_cost: int = 1,
) -> SupervisorSnapshot:
    """Close POSTMORTEM -> MEMORY -> NEXT_QUESTION -> COMPLETE durably.

    Negative/null/harmful outcomes must carry the canonical durable Postmortem emitted
    by the factory bridge. The caller supplies the next ResearchQuestion; it is
    appended idempotently to ScientificRegistry before the final supervisor binding.
    Thus a restart anywhere in MEMORY/NEXT_QUESTION can be replayed without inventing
    a second question or silently accepting a conflicting one.
    """

    if not isinstance(supervisor, ResearchSupervisor):
        raise TypeError("supervisor must be ResearchSupervisor")
    if not isinstance(decision, FinalizedFactoryDecision):
        raise TypeError("decision must be FinalizedFactoryDecision")
    if not isinstance(staged, StagedFactoryEvaluation):
        raise TypeError("staged must be StagedFactoryEvaluation")
    if not isinstance(next_question, ResearchQuestion):
        raise TypeError("next_question must be ResearchQuestion")

    promotion = supervisor.scientific_registry.get(
        "PromotionDecision", decision.promotion_decision_id
    )
    if promotion is None:
        raise ResearchSupervisorError(
            "factory decision is not durable in ScientificRegistry"
        )
    if promotion.payload.get("candidate_strategy_version_id") != staged.strategy_version_id:
        raise ResearchSupervisorError("durable promotion strategy identity mismatch")
    if promotion.payload.get("evaluation_bundle_id") != staged.evaluation_bundle_id:
        raise ResearchSupervisorError("durable promotion evaluation identity mismatch")
    if promotion.payload.get("candidate_model_version_id") != staged.model_version_id:
        raise ResearchSupervisorError("durable promotion model identity mismatch")

    postmortem_bindings: tuple[tuple[str, str], ...] = ()
    if decision.action is PromotionAction.REJECT:
        if decision.postmortem_id is None:
            raise ResearchSupervisorError(
                "rejected factory decision lacks durable postmortem identity"
            )
        postmortem = supervisor.scientific_registry.get(
            "Postmortem", decision.postmortem_id
        )
        if postmortem is None:
            raise ResearchSupervisorError(
                "rejected factory decision postmortem is not durable"
            )
        if postmortem.payload.get("experiment_id") != staged.experiment_id:
            raise ResearchSupervisorError("postmortem experiment identity mismatch")
        postmortem_bindings = (("postmortem_id", decision.postmortem_id),)
    elif decision.postmortem_id is not None:
        raise ResearchSupervisorError(
            "promoted factory decision cannot carry a negative-result postmortem"
        )

    snapshot = _advance_once_or_replay(
        supervisor,
        run_id,
        expected_phase=ResearchPhase.POSTMORTEM,
        next_phase=ResearchPhase.MEMORY,
        at=at,
        budget_cost=postmortem_budget_cost,
        bindings=postmortem_bindings,
    )

    memory_bindings = (
        ("promotion_decision_id", decision.promotion_decision_id),
        ("experiment_id", staged.experiment_id),
    )
    if snapshot.phase is ResearchPhase.MEMORY:
        snapshot = supervisor.advance(
            run_id,
            expected_phase=ResearchPhase.MEMORY,
            at=at,
            budget_cost=memory_budget_cost,
            bindings=memory_bindings,
        )
    else:
        snapshot = _require_replayed_bindings(
            snapshot,
            required=memory_bindings,
            at_least_phase=ResearchPhase.NEXT_QUESTION,
        )

    supervisor.scientific_registry.append(next_question)
    question_bindings = (("next_question_id", next_question.record_id),)
    if snapshot.phase is ResearchPhase.NEXT_QUESTION:
        snapshot = supervisor.advance(
            run_id,
            expected_phase=ResearchPhase.NEXT_QUESTION,
            at=at,
            budget_cost=next_question_budget_cost,
            bindings=question_bindings,
        )
    else:
        snapshot = _require_replayed_bindings(
            snapshot,
            required=question_bindings,
            at_least_phase=ResearchPhase.COMPLETE,
        )
    return snapshot
