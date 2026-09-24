from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext

import pytest

from autosport.learning_environment import (
    Action,
    EvidenceTruth,
    LearningEnvironmentError,
    Outcome,
    RewardEvidence,
    Transition,
)
from autosport.transparent_bandit_policy import ActionEstimate, BanditPolicyState


_ENVIRONMENT_ID = "a" * 64
_OBSERVATION_ID = "b" * 64
_EPISODE_ID = "c" * 64
_CONFIG_SHA256 = "d" * 64


def _causal_reward(reward_text: str):
    action = Action(
        environment_id=_ENVIRONMENT_ID,
        observation_id=_OBSERVATION_ID,
        action_type="WAIT",
        decided_at="2026-09-22T02:00:00Z",
        parameters=(("reason", "no-edge"),),
    )
    outcome = Outcome(
        environment_id=_ENVIRONMENT_ID,
        action_id=action.action_id,
        revealed_at="2026-09-22T02:01:00Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("settlement", "no-effect"),),
    )
    reward = RewardEvidence(
        environment_id=_ENVIRONMENT_ID,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal(reward_text),
        available_at="2026-09-22T02:01:01Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("owner_utility", "zero-or-exact"),),
    )
    transition = Transition(
        environment_id=_ENVIRONMENT_ID,
        episode_id=_EPISODE_ID,
        step_index=1,
        observation_id=action.observation_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at="2026-09-22T02:01:02Z",
    )
    return action, reward, transition


def _initial_policy() -> BanditPolicyState:
    return BanditPolicyState.initial(
        environment_id=_ENVIRONMENT_ID,
        protocol_id="reward-alias-adversarial-v1",
        config_sha256=_CONFIG_SHA256,
        seed=17,
        action_types=frozenset({"WAIT"}),
    )


@pytest.mark.parametrize(
    ("left_text", "right_text"),
    (
        ("0.25", "0.2500"),
        ("1", "1.000"),
        ("0", "-0.0000"),
        ("-2.5", "-2.50000"),
    ),
)
def test_decimal_reward_aliases_cannot_mint_distinct_causal_or_policy_identity(
    left_text: str,
    right_text: str,
) -> None:
    left_action, left_reward, left_transition = _causal_reward(left_text)
    right_action, right_reward, right_transition = _causal_reward(right_text)

    assert left_reward.reward == right_reward.reward
    assert left_action.action_id == right_action.action_id

    # Numerically identical exact rewards are the same causal fact. Decimal spelling
    # must not create a second reward/transition identity or a second learner branch.
    assert left_reward.reward_id == right_reward.reward_id
    assert left_transition.transition_id == right_transition.transition_id

    policy = _initial_policy()
    left_successor, left_update = policy.update(
        action=left_action,
        reward=left_reward,
        transition=left_transition,
    )
    right_successor, right_update = policy.update(
        action=right_action,
        reward=right_reward,
        transition=right_transition,
    )

    assert left_successor.policy_id == right_successor.policy_id
    assert left_successor == right_successor
    assert left_update.update_id == right_update.update_id
    assert left_update == right_update


def test_alias_replay_cannot_amplify_an_already_applied_action() -> None:
    action, reward, transition = _causal_reward("0.25")
    successor, _ = _initial_policy().update(
        action=action,
        reward=reward,
        transition=transition,
    )
    alias_action, alias_reward, alias_transition = _causal_reward("0.2500")

    with pytest.raises(LearningEnvironmentError, match="already has"):
        successor.update(
            action=alias_action,
            reward=alias_reward,
            transition=alias_transition,
        )




def test_common_integer_reward_sum_keeps_fixed_canonical_text() -> None:
    fixed = ActionEstimate(
        action_type="WAIT",
        observations=1,
        reward_sum=Decimal("1000.000"),
    )
    scientific_alias = ActionEstimate(
        action_type="WAIT",
        observations=1,
        reward_sum=Decimal("1E+3"),
    )

    assert fixed.to_payload() == scientific_alias.to_payload()
    assert fixed.to_payload()["reward_sum"] == "1000"


def test_reward_and_policy_identity_ignore_ambient_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        low_action, low_reward, low_transition = _causal_reward(
            "123456789.123456789000"
        )
        low_successor, low_update = _initial_policy().update(
            action=low_action,
            reward=low_reward,
            transition=low_transition,
        )

    with localcontext() as context:
        context.prec = 80
        context.rounding = ROUND_UP
        high_action, high_reward, high_transition = _causal_reward(
            "123456789.123456789"
        )
        high_successor, high_update = _initial_policy().update(
            action=high_action,
            reward=high_reward,
            transition=high_transition,
        )

    assert low_reward.reward_id == high_reward.reward_id
    assert low_transition.transition_id == high_transition.transition_id
    assert low_successor == high_successor
    assert low_successor.policy_id == high_successor.policy_id
    assert low_update.update_id == high_update.update_id


def test_canonical_reward_sum_identity_stays_compact_for_large_exponents() -> None:
    estimate = ActionEstimate(
        action_type="WAIT",
        observations=1,
        reward_sum=Decimal("1E+100000"),
    )
    alias = ActionEstimate(
        action_type="WAIT",
        observations=1,
        reward_sum=Decimal("10E+99999"),
    )

    payload = estimate.to_payload()
    assert payload == alias.to_payload()
    assert payload["reward_sum"] == "1E+100000"
    assert len(payload["reward_sum"]) < 32
