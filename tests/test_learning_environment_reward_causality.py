import unittest
from decimal import Decimal

from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    LearningEnvironmentError,
    Observation,
    Outcome,
    RewardEvidence,
)


class RewardRevealCausalityTests(unittest.TestCase):
    def test_reward_cannot_be_available_before_bound_outcome_reveal(self) -> None:
        identity = EnvironmentIdentity(
            source_id="paper-replay-source-v1",
            config_id="learning-config-v1",
            data_id="dataset-sha256-example",
            protocol_id="rq-learning-001",
            cutoff_ts="2026-09-17T14:00:00+00:00",
            seed=17,
        )
        environment = CausalLearningEnvironment(
            identity,
            episode_key="episode-reward-ordering",
            policy_id="policy-transparent-v1",
            admissible_actions=frozenset({"WAIT"}),
        )
        observation = Observation(
            environment_id=environment.environment_id,
            observed_at="2026-09-17T13:00:00+00:00",
            available_at="2026-09-17T13:00:01+00:00",
            evidence=(("market_state", "snapshot-17"),),
        )
        action = environment.act(
            observation,
            action_type="WAIT",
            decision_at="2026-09-17T13:00:02+00:00",
        )
        outcome = Outcome(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-17T13:05:00+00:00",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("settlement", "paper-noop"),),
        )
        reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0"),
            available_at="2026-09-17T13:04:59+00:00",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("reward_rule", "paper-return-v1"),),
        )

        with self.assertRaisesRegex(LearningEnvironmentError, "before outcome reveal"):
            environment.resolve(
                action.action_id,
                outcome=outcome,
                reward=reward,
                resolved_at="2026-09-17T13:05:01+00:00",
            )


if __name__ == "__main__":
    unittest.main()
