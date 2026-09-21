from __future__ import annotations

from dataclasses import fields as dataclass_fields
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib

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
from autosport.policy_update_authority import (
    UTILITY_AUTHORITY_UNRESOLVED,
    attempt_utility_bound_update,
)
from autosport.policy_utility_evidence import (
    DecisionKind,
    PolicyUtilityEvidence,
    UtilityCompleteness,
    UtilityTruthClass,
)
from autosport.transparent_bandit_policy import BanditPolicyState


UTC_NOW = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


def _resolved_step():
    identity = EnvironmentIdentity(
        source_id="paper-replay-source-v1",
        config_id="learning-config-v1",
        data_id="dataset-sha256-example",
        protocol_id="rq-learning-utility-001",
        cutoff_ts="2026-09-20T14:01:01Z",
        seed=31,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="episode-utility-gate",
        policy_id="transparent-bandit-bootstrap-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-20T14:01:00Z",
        available_at="2026-09-20T14:01:01Z",
        evidence=(("quote", "2.10"),),
    )
    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at="2026-09-20T14:01:02Z",
    )
    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at="2026-09-20T14:10:00Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("result", "home-win"),),
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at="2026-09-20T14:10:01Z",
        truth=EvidenceTruth.SIMULATED,
        evidence=(("paper_execution", "modelled-fill"),),
        simulation_model_id="paper-fill-return-model-v1",
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at="2026-09-20T14:10:02Z",
    )
    config_sha256 = hashlib.sha256(b"utility-gate-config-v1").hexdigest()
    policy = BanditPolicyState.initial(
        environment_id=environment.environment_id,
        protocol_id="rq-learning-utility-001",
        config_sha256=config_sha256,
        seed=31,
        action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    return policy, action, reward, transition


def _utility(policy, action, reward, transition, **overrides):
    values = {
        "environment_id": policy.environment_id,
        "episode_id": transition.episode_id,
        "action_id": action.action_id,
        "outcome_id": reward.outcome_id,
        "reward_id": reward.reward_id,
        "transition_id": transition.transition_id,
        "policy_id": policy.policy_id,
        "model_id": "transparent-bandit",
        "strategy_id": "paper-proposal",
        "config_sha256": policy.config_sha256,
        "protocol_sha256": hashlib.sha256(b"rq-learning-utility-001").hexdigest(),
        "economic_goal_fingerprint": hashlib.sha256(b"goal").hexdigest(),
        "risk_fingerprint": hashlib.sha256(b"risk").hexdigest(),
        "bankroll_id": "paper-bankroll",
        "portfolio_identity": "paper-portfolio",
        "utility_definition_family": "owner-net-utility",
        "utility_definition_version": "v1",
        "utility_definition_sha256": hashlib.sha256(b"owner-net-utility-v1").hexdigest(),
        "completeness": UtilityCompleteness.INCOMPLETE,
        "truth_class": UtilityTruthClass.OBSERVED,
        "decision_kind": DecisionKind.POSITIONED,
        "available_at": UTC_NOW,
        "currency": "EUR",
        "utility_value": Decimal("0.25"),
    }
    values.update(overrides)
    return PolicyUtilityEvidence(**values)


def _subclass_copy(value):
    forged_type = type(f"Forged{type(value).__name__}", (type(value),), {})
    return forged_type(
        **{
            field.name: getattr(value, field.name)
            for field in dataclass_fields(value)
            if field.init
        }
    )


def test_utility_gate_blocks_raw_reward_update_and_is_deterministic() -> None:
    policy, action, reward, transition = _resolved_step()
    utility = _utility(policy, action, reward, transition)

    successor, evidence = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )
    repeat_successor, repeat_evidence = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )

    assert successor == policy
    assert repeat_successor == policy
    assert successor.generation == 0
    assert evidence.reason_codes == (UTILITY_AUTHORITY_UNRESOLVED,)
    assert repeat_evidence == evidence
    assert repeat_evidence.evidence_id == evidence.evidence_id
    assert evidence.predecessor_policy_id == evidence.successor_policy_id == policy.policy_id
    assert evidence.utility_evidence_id == utility.evidence_id


def test_utility_gate_records_exact_binding_mismatch_without_mutation() -> None:
    policy, action, reward, transition = _resolved_step()
    utility = _utility(policy, action, reward, transition, reward_id="not-the-reward")

    successor, evidence = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )

    assert successor == policy
    assert successor.generation == 0
    assert evidence.reason_codes == (
        UTILITY_AUTHORITY_UNRESOLVED,
        "utility_reward_mismatch",
    )


def test_utility_gate_reuses_generic_learner_causal_admission_boundary() -> None:
    policy, action, reward, transition = _resolved_step()
    utility = _utility(policy, action, reward, transition)
    foreign_environment = "f" * 64
    other_identity = "e" * 64

    malformed = (
        (replace(action, environment_id=foreign_environment), reward, transition),
        (action, replace(reward, environment_id=foreign_environment), transition),
        (action, reward, replace(transition, environment_id=foreign_environment)),
        (action, replace(reward, action_id=other_identity), transition),
        (action, reward, replace(transition, action_id=other_identity)),
        (action, reward, replace(transition, observation_id=other_identity)),
        (action, reward, replace(transition, outcome_id=other_identity)),
        (action, reward, replace(transition, reward_id=other_identity)),
    )

    for bad_action, bad_reward, bad_transition in malformed:
        with pytest.raises(LearningEnvironmentError):
            attempt_utility_bound_update(
                policy=policy,
                action=bad_action,
                reward=bad_reward,
                transition=bad_transition,
                utility=utility,
            )


@pytest.mark.parametrize("forged_component", ("policy", "action", "reward", "transition", "utility"))
def test_utility_gate_rejects_polymorphic_authority_inputs(forged_component: str) -> None:
    policy, action, reward, transition = _resolved_step()
    utility = _utility(policy, action, reward, transition)
    arguments = {
        "policy": policy,
        "action": action,
        "reward": reward,
        "transition": transition,
        "utility": utility,
    }
    arguments[forged_component] = _subclass_copy(arguments[forged_component])

    with pytest.raises(TypeError, match="exact|canonical"):
        attempt_utility_bound_update(**arguments)
