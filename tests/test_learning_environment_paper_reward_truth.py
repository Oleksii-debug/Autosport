import unittest
from decimal import Decimal

from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)


class PaperRewardTruthBoundaryTests(unittest.TestCase):
    def test_observed_result_can_feed_model_bound_simulated_paper_reward(self) -> None:
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
            episode_key="episode-observed-result-simulated-reward",
            policy_id="policy-transparent-v1",
            admissible_actions=frozenset({"PAPER_PROPOSAL"}),
        )
        observation = Observation(
            environment_id=environment.environment_id,
            observed_at="2026-09-17T13:00:00+00:00",
            available_at="2026-09-17T13:00:01+00:00",
            evidence=(("quote", "2.10"),),
        )
        action = environment.act(
            observation,
            action_type="PAPER_PROPOSAL",
            decision_at="2026-09-17T13:00:02+00:00",
            parameters=(("candidate_id", "paper-candidate-1"),),
        )
        outcome = Outcome(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-17T13:05:00+00:00",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("match_result", "home-win"),),
        )
        reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0.25"),
            available_at="2026-09-17T13:05:01+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("paper_execution", "modelled-fill"),),
            simulation_model_id="paper-fill-return-model-v1",
        )

        transition = environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=reward,
            resolved_at="2026-09-17T13:05:02+00:00",
        )

        self.assertEqual(transition.outcome_id, outcome.outcome_id)
        self.assertEqual(transition.reward_id, reward.reward_id)
        self.assertIs(outcome.truth, EvidenceTruth.OBSERVED)
        self.assertIs(reward.truth, EvidenceTruth.SIMULATED)
        self.assertEqual(reward.simulation_model_id, "paper-fill-return-model-v1")


if __name__ == "__main__":
    unittest.main()
