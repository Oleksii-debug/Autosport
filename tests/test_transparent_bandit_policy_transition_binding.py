from dataclasses import replace
from decimal import Decimal

import pytest

import autosport.transparent_bandit_policy as policy_module
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

def test_update_rejects_action_subclass_before_identity_dispatch():
    policy, action, reward, transition = _resolved_step()

    class HostileAction(type(action)):
        @property
        def action_id(self):
            raise AssertionError("Action subclass identity dispatch executed")

    hostile = HostileAction(
        environment_id=action.environment_id,
        observation_id=action.observation_id,
        action_type=action.action_type,
        decided_at=action.decided_at,
        parameters=action.parameters,
    )

    with pytest.raises(TypeError, match="exact Action"):
        policy.update(action=hostile, reward=reward, transition=transition)


def test_update_rejects_reward_subclass_before_identity_dispatch():
    policy, action, reward, transition = _resolved_step()

    class HostileReward(type(reward)):
        @property
        def reward_id(self):
            raise AssertionError("RewardEvidence subclass identity dispatch executed")

    hostile = HostileReward(
        environment_id=reward.environment_id,
        action_id=reward.action_id,
        outcome_id=reward.outcome_id,
        reward=reward.reward,
        available_at=reward.available_at,
        truth=reward.truth,
        evidence=reward.evidence,
        simulation_model_id=reward.simulation_model_id,
    )

    with pytest.raises(TypeError, match="exact RewardEvidence"):
        policy.update(action=action, reward=hostile, transition=transition)


def test_update_rejects_transition_subclass_before_identity_dispatch():
    policy, action, reward, transition = _resolved_step()

    class HostileTransition(type(transition)):
        @property
        def transition_id(self):
            raise AssertionError("Transition subclass identity dispatch executed")

    hostile = HostileTransition(
        environment_id=transition.environment_id,
        episode_id=transition.episode_id,
        step_index=transition.step_index,
        observation_id=transition.observation_id,
        action_id=transition.action_id,
        outcome_id=transition.outcome_id,
        reward_id=transition.reward_id,
        decision_at=transition.decision_at,
        resolved_at=transition.resolved_at,
    )

    with pytest.raises(TypeError, match="exact Transition"):
        policy.update(action=action, reward=reward, transition=hostile)

def test_update_evidence_rejects_rebound_action_chain_with_rewritten_top_level_id():
    policy, action, reward, transition = _resolved_step()
    _successor, evidence = policy.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    rebound_action = replace(
        action,
        decided_at="2026-09-17T13:00:03Z",
    )

    with pytest.raises(LearningEnvironmentError, match="bind the exact action"):
        replace(
            evidence,
            action=rebound_action,
            action_id=rebound_action.action_id,
        )


def test_update_evidence_rejects_rebound_reward_chain_with_rewritten_top_level_id():
    policy, action, reward, transition = _resolved_step()
    _successor, evidence = policy.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    rebound_reward = replace(
        reward,
        reward=Decimal("0.30"),
    )

    with pytest.raises(LearningEnvironmentError, match="bind the exact reward"):
        replace(
            evidence,
            reward=rebound_reward,
            reward_id=rebound_reward.reward_id,
        )



def test_update_rejects_rebound_action_type_root_before_dispatch(monkeypatch):
    policy, _action, reward, transition = _resolved_step()
    hostile_calls: list[str] = []

    class ForgedAction:
        def __getattribute__(self, name):
            hostile_calls.append(name)
            raise AssertionError("forged Action dispatch executed")

    forged = object.__new__(ForgedAction)
    monkeypatch.setattr(policy_module, "Action", ForgedAction)

    with pytest.raises(
        LearningEnvironmentError,
        match="causal witness type authority changed",
    ):
        policy.update(action=forged, reward=reward, transition=transition)

    assert hostile_calls == []


def test_update_evidence_rejects_rebound_action_type_root_before_dispatch(
    monkeypatch,
):
    policy, action, reward, transition = _resolved_step()
    _successor, evidence = policy.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    hostile_calls: list[str] = []

    class ForgedAction:
        def __getattribute__(self, name):
            hostile_calls.append(name)
            raise AssertionError("forged evidence Action dispatch executed")

    forged = object.__new__(ForgedAction)
    monkeypatch.setattr(policy_module, "Action", ForgedAction)

    with pytest.raises(
        LearningEnvironmentError,
        match="causal witness type authority changed",
    ):
        replace(evidence, action=forged)

    assert hostile_calls == []


def test_update_evidence_cannot_mint_without_exact_causal_witnesses():
    policy, action, reward, transition = _resolved_step()
    _successor, evidence = policy.update(
        action=action,
        reward=reward,
        transition=transition,
    )

    with pytest.raises(
        LearningEnvironmentError,
        match="causal witnesses are required",
    ):
        replace(
            evidence,
            action=None,
            reward=None,
            transition=None,
        )
