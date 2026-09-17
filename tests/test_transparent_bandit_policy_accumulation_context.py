from decimal import Decimal, localcontext

from autosport.learning_environment import Action, EvidenceTruth, RewardEvidence, Transition
from autosport.transparent_bandit_policy import BanditPolicyState


def test_policy_update_is_independent_of_process_decimal_precision():
    environment_id = "a" * 64
    action = Action(
        environment_id=environment_id,
        observation_id="b" * 64,
        action_type="WAIT",
        decided_at="2026-09-17T13:00:00Z",
    )
    reward = RewardEvidence(
        environment_id=environment_id,
        action_id=action.action_id,
        outcome_id="c" * 64,
        reward=Decimal("0.25"),
        available_at="2026-09-17T13:05:00Z",
        truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=environment_id,
        episode_id="d" * 64,
        step_index=1,
        observation_id=action.observation_id,
        action_id=action.action_id,
        outcome_id=reward.outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at="2026-09-17T13:05:01Z",
    )
    policy = BanditPolicyState.initial(
        environment_id=environment_id,
        protocol_id="protocol-decimal-context-v1",
        config_sha256="e" * 64,
        seed=17,
        action_types=frozenset({"WAIT"}),
    )

    with localcontext() as context:
        context.prec = 1
        low_precision_successor, low_precision_evidence = policy.update(
            action=action,
            reward=reward,
            transition=transition,
        )

    with localcontext() as context:
        context.prec = 50
        high_precision_successor, high_precision_evidence = policy.update(
            action=action,
            reward=reward,
            transition=transition,
        )

    assert low_precision_successor == high_precision_successor
    assert low_precision_evidence == high_precision_evidence
    assert low_precision_successor.policy_id == high_precision_successor.policy_id
    assert low_precision_successor.estimates[0].reward_sum == Decimal("0.25")
