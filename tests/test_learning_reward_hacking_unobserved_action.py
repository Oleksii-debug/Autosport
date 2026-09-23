from decimal import Decimal

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


def test_missing_wait_reward_does_not_become_zero_utility():
    policy = _policy(
        paper_observations=1,
        paper_reward_sum="-1",
        wait_observations=0,
        wait_reward_sum="0",
    )

    assert policy.choose(
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"})
    ) == "PAPER_PROPOSAL"


def test_explicit_observed_zero_wait_reward_can_beat_observed_loss():
    policy = _policy(
        paper_observations=1,
        paper_reward_sum="-1",
        wait_observations=1,
        wait_reward_sum="0",
    )

    assert policy.choose(
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"})
    ) == "WAIT"


def test_external_admissibility_can_explicitly_route_unobserved_action():
    policy = _policy(
        paper_observations=1,
        paper_reward_sum="-1",
        wait_observations=0,
        wait_reward_sum="0",
    )

    assert policy.choose(admissible_actions=frozenset({"WAIT"})) == "WAIT"
