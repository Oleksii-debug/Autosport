"""Identity-only glue for the canonical Autosport closed learning loop.

This module introduces no scheduler, scientific registry, model factory, risk authority,
or execution authority. It binds evidence already durably owned by AgentLoop,
NightResearchCurriculum, ResearchSupervisor, ScientificRegistry and the canonical
Strategy/Model Factory into one immutable challenger identity.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final

from .agent_loop import AgentLoopSnapshot
from .learning_environment import EvidenceTruth
from .research_curriculum import (
    CurriculumDispatchReceipt,
    CurriculumSelectionRecord,
    NightResearchCurriculum,
    ReplayProvenance,
)
from .research_factory_bridge import StagedFactoryEvaluation
from .research_supervisor import ResearchSupervisor
from .scientific_registry import ScientificRegistry
from .strategy_model_factory import FactoryCandidateSpec

SCHEMA: Final = "autosport.closed_loop_challenger"
SCHEMA_VERSION: Final = 1
_HEX: Final = frozenset("0123456789abcdef")


class ClosedLoopBindingError(RuntimeError):
    """Canonical closed-loop evidence is incomplete, stale, or inconsistent."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ClosedLoopBindingError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ClosedLoopBindingError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ClosedLoopBindingError(f"{name} must be canonical SHA-256 hex")
    return text


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ClosedLoopBindingError("challenger payload is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChallengerArtifact:
    """Immutable identity bridge from one causal episode to one factory challenger."""

    research_question_id: str
    originating_research_run_id: str
    curriculum_run_id: str
    curriculum_selection_id: str
    replay_evidence_binding_id: str
    replay_provenance: ReplayProvenance
    evidence_truth: EvidenceTruth
    environment_id: str
    episode_id: str
    transition_id: str
    reward_id: str
    agent_loop_state_sha256: str
    research_protocol_id: str
    experiment_id: str
    model_version_id: str
    strategy_version_id: str
    evaluation_bundle_id: str
    promotion_decision_id: str
    economic_goal_fingerprint: str
    risk_fingerprint: str
    source_sha256: str
    config_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "research_question_id",
            "originating_research_run_id",
            "curriculum_run_id",
            "research_protocol_id",
            "experiment_id",
            "model_version_id",
            "strategy_version_id",
            "evaluation_bundle_id",
            "promotion_decision_id",
        ):
            _text(getattr(self, name), name)
        for name in (
            "curriculum_selection_id",
            "replay_evidence_binding_id",
            "environment_id",
            "episode_id",
            "transition_id",
            "reward_id",
            "agent_loop_state_sha256",
            "economic_goal_fingerprint",
            "risk_fingerprint",
            "source_sha256",
            "config_sha256",
        ):
            _sha256(getattr(self, name), name)
        if not isinstance(self.replay_provenance, ReplayProvenance):
            raise ClosedLoopBindingError("replay_provenance must be ReplayProvenance")
        if self.replay_provenance is ReplayProvenance.REAL_EXECUTION:
            raise ClosedLoopBindingError(
                "paper/shadow ChallengerArtifact cannot claim REAL_EXECUTION provenance"
            )
        if not isinstance(self.evidence_truth, EvidenceTruth):
            raise ClosedLoopBindingError("evidence_truth must be EvidenceTruth")

    def payload(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": "ChallengerArtifact",
            "research_question_id": self.research_question_id,
            "originating_research_run_id": self.originating_research_run_id,
            "curriculum_run_id": self.curriculum_run_id,
            "curriculum_selection_id": self.curriculum_selection_id,
            "replay_evidence_binding_id": self.replay_evidence_binding_id,
            "replay_provenance": self.replay_provenance.value,
            "evidence_truth": self.evidence_truth.value,
            "environment_id": self.environment_id,
            "episode_id": self.episode_id,
            "transition_id": self.transition_id,
            "reward_id": self.reward_id,
            "agent_loop_state_sha256": self.agent_loop_state_sha256,
            "research_protocol_id": self.research_protocol_id,
            "experiment_id": self.experiment_id,
            "model_version_id": self.model_version_id,
            "strategy_version_id": self.strategy_version_id,
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "promotion_decision_id": self.promotion_decision_id,
            "economic_goal_fingerprint": self.economic_goal_fingerprint,
            "risk_fingerprint": self.risk_fingerprint,
            "source_sha256": self.source_sha256,
            "config_sha256": self.config_sha256,
        }

    @property
    def artifact_id(self) -> str:
        return _digest(self.payload())


def bind_challenger_artifact(
    *,
    loop_snapshot: AgentLoopSnapshot,
    curriculum: NightResearchCurriculum,
    selection: CurriculumSelectionRecord,
    dispatch: CurriculumDispatchReceipt,
    supervisor: ResearchSupervisor,
    registry: ScientificRegistry,
    spec: FactoryCandidateSpec,
    staged: StagedFactoryEvaluation,
) -> ChallengerArtifact:
    """Bind already-canonical evidence without creating a new authority."""

    if not isinstance(loop_snapshot, AgentLoopSnapshot):
        raise TypeError("loop_snapshot must be AgentLoopSnapshot")
    if not isinstance(curriculum, NightResearchCurriculum):
        raise TypeError("curriculum must be NightResearchCurriculum")
    if not isinstance(selection, CurriculumSelectionRecord):
        raise TypeError("selection must be CurriculumSelectionRecord")
    if not isinstance(dispatch, CurriculumDispatchReceipt):
        raise TypeError("dispatch must be CurriculumDispatchReceipt")
    if not isinstance(supervisor, ResearchSupervisor):
        raise TypeError("supervisor must be ResearchSupervisor")
    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be ScientificRegistry")
    if not isinstance(spec, FactoryCandidateSpec):
        raise TypeError("spec must be FactoryCandidateSpec")
    if not isinstance(staged, StagedFactoryEvaluation):
        raise TypeError("staged must be StagedFactoryEvaluation")

    question_id = loop_snapshot.research_question_id
    origin_run_id = loop_snapshot.research_run_id
    transition_id = loop_snapshot.transition_id
    reward_id = loop_snapshot.reward_id
    if question_id is None or origin_run_id is None:
        raise ClosedLoopBindingError("AgentLoop lacks durable research handoff identity")
    if transition_id is None or reward_id is None:
        raise ClosedLoopBindingError("AgentLoop lacks resolved causal transition identity")
    if selection.selected_question_id != question_id:
        raise ClosedLoopBindingError(
            "curriculum selection does not bind AgentLoop-generated ResearchQuestion"
        )
    if selection.selected_episode_id != loop_snapshot.episode_id:
        raise ClosedLoopBindingError("curriculum selection episode differs from AgentLoop")
    if dispatch.selection_id != selection.selection_id:
        raise ClosedLoopBindingError("curriculum dispatch does not bind exact selection")

    curriculum_state = curriculum.snapshot()
    expected_selection = {**selection.payload(), "selection_id": selection.selection_id}
    if expected_selection not in curriculum_state.get("selections", []):
        raise ClosedLoopBindingError("selection is not exact durable curriculum evidence")
    persisted_dispatch = curriculum_state.get("dispatches", {}).get(selection.selection_id)
    if (
        type(persisted_dispatch) is not dict
        or persisted_dispatch.get("status") != "ACCEPTED"
        or persisted_dispatch.get("run_id") != dispatch.run_id
        or persisted_dispatch.get("receipt_sha256")
        != dispatch.trigger_receipt.receipt_sha256
    ):
        raise ClosedLoopBindingError("dispatch is not exact durable curriculum evidence")

    origin = supervisor.status(origin_run_id)
    curriculum_run = supervisor.status(dispatch.run_id)
    if origin.question_id != question_id or curriculum_run.question_id != question_id:
        raise ClosedLoopBindingError("ResearchSupervisor run question identity mismatch")

    question = registry.get("ResearchQuestion", question_id)
    if question is None:
        raise ClosedLoopBindingError("AgentLoop ResearchQuestion is missing from ScientificRegistry")
    protocol = registry.get("ResearchProtocol", spec.research_protocol_id)
    if protocol is None:
        raise ClosedLoopBindingError("factory ResearchProtocol is missing")
    binding = protocol.payload.get("binding")
    if type(binding) is not dict or binding.get("research_question_id") != question_id:
        raise ClosedLoopBindingError(
            "factory protocol does not bind AgentLoop-generated ResearchQuestion"
        )

    expected_stage = {
        "experiment_id": spec.experiment_id,
        "model_version_id": spec.model_version_id,
        "strategy_version_id": spec.strategy_version_id,
        "evaluation_bundle_id": spec.evaluation_bundle_id,
        "promotion_decision_id": spec.promotion_decision_id,
    }
    for name, expected in expected_stage.items():
        if getattr(staged, name) != expected:
            raise ClosedLoopBindingError(f"staged factory identity mismatch: {name}")

    experiment = registry.get("Experiment", spec.experiment_id)
    model = registry.get("ModelVersion", spec.model_version_id)
    strategy = registry.get("StrategyVersion", spec.strategy_version_id)
    bundle = registry.get("EvaluationBundle", spec.evaluation_bundle_id)
    if any(value is None for value in (experiment, model, strategy, bundle)):
        raise ClosedLoopBindingError("staged factory lineage is incomplete in ScientificRegistry")
    assert experiment is not None and model is not None and strategy is not None and bundle is not None
    if experiment.payload.get("research_protocol_id") != spec.research_protocol_id:
        raise ClosedLoopBindingError("staged Experiment protocol identity mismatch")
    if experiment.payload.get("strategy_version_id") != spec.strategy_version_id:
        raise ClosedLoopBindingError("staged Experiment strategy identity mismatch")
    if experiment.payload.get("model_version_id") != spec.model_version_id:
        raise ClosedLoopBindingError("staged Experiment model identity mismatch")
    if strategy.payload.get("environment_sha256") != spec.environment_sha256.lower():
        raise ClosedLoopBindingError("challenger strategy environment identity mismatch")
    if model.payload.get("research_protocol_id") != spec.research_protocol_id:
        raise ClosedLoopBindingError("challenger model protocol identity mismatch")
    if bundle.payload.get("evaluated_strategy_version_id") != spec.strategy_version_id:
        raise ClosedLoopBindingError("evaluation bundle strategy identity mismatch")
    if bundle.payload.get("evaluated_model_version_id") != spec.model_version_id:
        raise ClosedLoopBindingError("evaluation bundle model identity mismatch")

    return ChallengerArtifact(
        research_question_id=question_id,
        originating_research_run_id=origin_run_id,
        curriculum_run_id=dispatch.run_id,
        curriculum_selection_id=selection.selection_id,
        replay_evidence_binding_id=selection.selected_evidence_binding_id,
        replay_provenance=selection.selected_provenance,
        evidence_truth=selection.selected_evidence_truth,
        environment_id=loop_snapshot.environment_id,
        episode_id=loop_snapshot.episode_id,
        transition_id=transition_id,
        reward_id=reward_id,
        agent_loop_state_sha256=loop_snapshot.state_sha256,
        research_protocol_id=spec.research_protocol_id,
        experiment_id=spec.experiment_id,
        model_version_id=spec.model_version_id,
        strategy_version_id=spec.strategy_version_id,
        evaluation_bundle_id=spec.evaluation_bundle_id,
        promotion_decision_id=spec.promotion_decision_id,
        economic_goal_fingerprint=loop_snapshot.economic_goal_fingerprint,
        risk_fingerprint=loop_snapshot.risk_fingerprint,
        source_sha256=loop_snapshot.source_sha256,
        config_sha256=loop_snapshot.config_sha256,
    )
