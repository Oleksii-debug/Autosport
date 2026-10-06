from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    LearningEnvironmentError,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.transparent_bandit_policy import BanditPolicyState


def _resolved_step():
    identity = EnvironmentIdentity(
        source_id="paper-replay-source-v1",
        config_id="learning-config-v1",
        data_id="dataset-sha256-example",
        protocol_id="rq-learning-001",
        cutoff_ts="2026-09-17T14:00:00Z",
        seed=17,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="episode-transition-binding",
        policy_id="transparent-bandit-bootstrap-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL"}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-17T13:00:00Z",
        available_at="2026-09-17T13:00:01Z",
        evidence=(("quote", "2.10"),),
    )
    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at="2026-09-17T13:00:02Z",
    )
    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at="2026-09-17T14:05:00Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("match_result", "home-win"),),
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at="2026-09-17T14:05:01Z",
        truth=EvidenceTruth.SIMULATED,
        evidence=(("paper_execution", "modelled-fill"),),
        simulation_model_id="paper-fill-return-model-v1",
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at="2026-09-17T14:05:02Z",
    )
    policy = BanditPolicyState.initial(
        environment_id=environment.environment_id,
        protocol_id="rq-learning-001",
        config_sha256="c" * 64,
        seed=17,
        action_types=frozenset({"PAPER_PROPOSAL"}),
    )
    return policy, action, reward, transition


def test_update_rejects_transition_with_substituted_observation_identity():
    policy, action, reward, transition = _resolved_step()
    corrupted = replace(transition, observation_id="0" * 64)

    with pytest.raises(LearningEnvironmentError, match="exact observation"):
        policy.update(action=action, reward=reward, transition=corrupted)


def test_update_rejects_transition_with_substituted_outcome_identity():
    policy, action, reward, transition = _resolved_step()
    corrupted = replace(transition, outcome_id="1" * 64)

    with pytest.raises(LearningEnvironmentError, match="exact outcome"):
        policy.update(action=action, reward=reward, transition=corrupted)
