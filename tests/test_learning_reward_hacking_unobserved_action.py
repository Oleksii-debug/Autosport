from decimal import Decimal

import pytest

from autosport.learning_environment import LearningEnvironmentError
from autosport.transparent_bandit_policy import ActionEstimate, BanditPolicyState


def _policy(
    *,
    paper_observations: int,
    paper_reward_sum: str,
    wait_observations: int,
    wait_reward_sum: str,
) -> BanditPolicyState:
    generation = paper_observations + wait_observations
    applied_action_ids = tuple(
        f"{index:064x}" for index in range(1, generation + 1)
    )
    applied_reward_ids = tuple(
        f"{index:064x}" for index in range(101, 101 + generation)
    )
    return BanditPolicyState(
        environment_id="a" * 64,
        protocol_id="protocol-wp-a02-unobserved-reward",
        config_sha256="b" * 64,
        seed=17,
        generation=generation,
        estimates=(
            ActionEstimate(
                "PAPER_PROPOSAL",
                paper_observations,
                Decimal(paper_reward_sum),
            ),
            ActionEstimate(
                "WAIT",
                wait_observations,
                Decimal(wait_reward_sum),
            ),
        ),
        applied_action_ids=applied_action_ids,
        applied_reward_ids=applied_reward_ids,
        predecessor_policy_id=None if generation == 0 else "c" * 64,
    )


def test_unobserved_action_has_no_empirical_mean_reward():
    estimate = ActionEstimate("WAIT", 0, Decimal("0"))

    with pytest.raises(LearningEnvironmentError, match="no empirical mean reward"):
        _ = estimate.mean_reward
    with pytest.raises(LearningEnvironmentError, match="no empirical mean reward"):
        _ = estimate.exact_mean_reward

    assert estimate.selection_score == 0


def test_explicit_observed_zero_remains_empirical_zero_reward():
    estimate = ActionEstimate("WAIT", 1, Decimal("0"))

    assert estimate.mean_reward == Decimal("0")
    assert estimate.exact_mean_reward == 0
    assert estimate.selection_score == 0


def test_neutral_bootstrap_selection_does_not_mint_wait_observation():
    policy = _policy(
        paper_observations=1,
        paper_reward_sum="-1",
        wait_observations=0,
        wait_reward_sum="0",
    )

    assert policy.choose(
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"})
    ) == "WAIT"

    wait_estimate = next(
        estimate for estimate in policy.estimates if estimate.action_type == "WAIT"
    )
    assert wait_estimate.observations == 0
    with pytest.raises(LearningEnvironmentError, match="no empirical mean reward"):
        _ = wait_estimate.mean_reward


def test_external_admissibility_can_explicitly_route_unobserved_action():
    policy = _policy(
        paper_observations=1,
        paper_reward_sum="-1",
        wait_observations=0,
        wait_reward_sum="0",
    )

    assert policy.choose(admissible_actions=frozenset({"WAIT"})) == "WAIT"


def test_all_unobserved_bootstrap_remains_deterministic_and_lexical():
    policy = _policy(
        paper_observations=0,
        paper_reward_sum="0",
        wait_observations=0,
        wait_reward_sum="0",
    )

    assert policy.choose(
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"})
    ) == "PAPER_PROPOSAL"
