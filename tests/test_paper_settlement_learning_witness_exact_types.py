from decimal import Decimal

import pytest

from autosport.learning_environment import (
    Action,
    EnvironmentCheckpoint,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)
from autosport.paper_settlement_learning import PaperSettlementLearningWitness


def _graph(observation_type=Observation):
    environment_id = "a" * 64
    episode_id = "b" * 64
    observation = observation_type(
        environment_id=environment_id,
        observed_at="2026-10-07T00:00:00Z",
        available_at="2026-10-07T00:00:01Z",
        evidence=(),
    )
    action = Action(
        environment_id=environment_id,
        observation_id=observation.observation_id,
        action_type="PAPER_PROPOSAL",
        decided_at="2026-10-07T00:00:02Z",
        parameters=(),
    )
    outcome = Outcome(
        environment_id=environment_id,
        action_id=action.action_id,
        revealed_at="2026-10-07T00:00:03Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(),
    )
    reward = RewardEvidence(
        environment_id=environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("1"),
        available_at="2026-10-07T00:00:04Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(),
    )
    transition = Transition(
        environment_id=environment_id,
        episode_id=episode_id,
        step_index=1,
        observation_id=observation.observation_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward_id=reward.reward_id,
        decision_at="2026-10-07T00:00:02Z",
        resolved_at="2026-10-07T00:00:04Z",
    )
    baseline = EnvironmentCheckpoint(
        environment_id=environment_id,
        episode_id=episode_id,
        policy_id="policy-1",
        step_index=0,
        chain_sha256="c" * 64,
        last_transition_id=None,
        committed_action_ids=(),
        committed_decision_intents=(),
    )
    next_checkpoint = EnvironmentCheckpoint(
        environment_id=environment_id,
        episode_id=episode_id,
        policy_id="policy-1",
        step_index=1,
        chain_sha256="d" * 64,
        last_transition_id=transition.transition_id,
        committed_action_ids=(action.action_id,),
        committed_decision_intents=(),
    )
    return observation, action, outcome, reward, transition, baseline, next_checkpoint


def test_witness_rejects_observation_subclass_before_identity_graph_dispatch() -> None:
    class ObservationAlias(Observation):
        pass

    observation, action, outcome, reward, transition, baseline, next_checkpoint = _graph(
        ObservationAlias
    )

    with pytest.raises(TypeError, match="exact canonical record type"):
        PaperSettlementLearningWitness(
            ticket_id="ticket-1",
            binding_id="e" * 64,
            settlement_bundle_sha256="f" * 64,
            observation=observation,
            action=action,
            outcome=outcome,
            reward=reward,
            transition=transition,
            baseline_checkpoint=baseline,
            next_checkpoint=next_checkpoint,
        )
