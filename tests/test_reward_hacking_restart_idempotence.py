from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import hashlib
import json

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


def _resolved_update():
    identity = EnvironmentIdentity(
        source_id="paper-replay-source-v1",
        config_id="reward-restart-idempotence-v1",
        data_id="dataset-restart-idempotence-v1",
        protocol_id="rq-reward-restart-idempotence-v1",
        cutoff_ts="2026-09-22T03:00:00Z",
        seed=73,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="episode-reward-restart-idempotence",
        policy_id="transparent-bandit-bootstrap-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-22T03:00:01Z",
        available_at="2026-09-22T03:00:02Z",
        evidence=(("quote", "2.20"),),
    )
    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at="2026-09-22T03:00:03Z",
    )
    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at="2026-09-22T03:10:00Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("result", "home-win"),),
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at="2026-09-22T03:10:01Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("paper_settlement", "settlement-1"),),
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at="2026-09-22T03:10:02Z",
    )
    policy = BanditPolicyState.initial(
        environment_id=environment.environment_id,
        protocol_id=identity.protocol_id,
        config_sha256=hashlib.sha256(b"reward-restart-idempotence-config-v1").hexdigest(),
        seed=73,
        action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    successor, evidence = policy.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    return action, reward, transition, successor, evidence


def _restart_roundtrip(policy: BanditPolicyState) -> BanditPolicyState:
    # Exercise the public JSON-shaped restart boundary rather than retaining
    # in-memory tuple/Decimal objects from the predecessor instance.
    durable_payload = json.loads(
        json.dumps(policy.to_payload(), sort_keys=True, separators=(",", ":"))
    )
    return BanditPolicyState.from_payload(durable_payload)


def test_restart_preserves_consumed_reward_and_blocks_exact_redelivery() -> None:
    action, reward, transition, successor, evidence = _resolved_update()

    restored = _restart_roundtrip(successor)

    assert restored == successor
    assert restored.policy_id == successor.policy_id == evidence.successor_policy_id
    assert restored.generation == 1
    assert restored.applied_action_ids == (action.action_id,)
    assert restored.applied_reward_ids == (reward.reward_id,)

    estimate = next(
        item for item in restored.estimates if item.action_type == action.action_type
    )
    assert estimate.observations == 1
    assert estimate.reward_sum == Decimal("0.25")

    with pytest.raises(LearningEnvironmentError, match="already has"):
        restored.update(
            action=action,
            reward=reward,
            transition=transition,
        )

    # The immutable restored state remains exactly one applied causal update.
    assert restored.generation == 1
    assert restored.policy_id == successor.policy_id
    assert next(
        item for item in restored.estimates if item.action_type == action.action_type
    ) == estimate


@pytest.mark.parametrize("history_field", ("applied_action_ids", "applied_reward_ids"))
def test_restart_rejects_partial_consumed_identity_history(history_field: str) -> None:
    _, _, _, successor, _ = _resolved_update()
    payload = deepcopy(successor.to_payload())

    # A crash/torn persistence representation cannot silently retain generation=1
    # while dropping one side of the consumed action/reward identity pair.
    payload[history_field] = []

    with pytest.raises(
        LearningEnvironmentError,
        match="equal length|generation must equal applied update count",
    ):
        BanditPolicyState.from_payload(payload)
