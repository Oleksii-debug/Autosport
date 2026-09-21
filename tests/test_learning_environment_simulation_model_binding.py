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


class SimulationModelBindingTests(unittest.TestCase):
    def _environment_and_action(self):
        environment = CausalLearningEnvironment(
            EnvironmentIdentity(
                source_id="simulation-source-v1",
                config_id="simulation-config-v1",
                data_id="simulation-data-v1",
                protocol_id="simulation-protocol-v1",
                cutoff_ts="2026-09-17T14:00:00+00:00",
                seed=17,
            ),
            episode_key="simulation-model-binding-episode",
            policy_id="simulation-model-binding-policy",
            admissible_actions=frozenset({"PAPER_PROPOSAL"}),
        )
        action = environment.act(
            Observation(
                environment_id=environment.environment_id,
                observed_at="2026-09-17T13:00:00+00:00",
                available_at="2026-09-17T13:00:01+00:00",
                evidence=(("quote", "2.10"),),
            ),
            action_type="PAPER_PROPOSAL",
            decision_at="2026-09-17T13:00:02+00:00",
        )
        return environment, action

    @staticmethod
    def _simulated_outcome(action, simulation_model_id: str) -> Outcome:
        return Outcome(
            environment_id=action.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-17T13:05:00+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("counterfactual", "modelled-fill"),),
            simulation_model_id=simulation_model_id,
        )

    @staticmethod
    def _simulated_reward(action, outcome: Outcome, simulation_model_id: str) -> RewardEvidence:
        return RewardEvidence(
            environment_id=action.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0.25"),
            available_at="2026-09-17T13:05:01+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("reward_rule", "simulated-return-v1"),),
            simulation_model_id=simulation_model_id,
        )

    def test_simulated_reward_cannot_switch_outcome_simulation_model(self) -> None:
        environment, action = self._environment_and_action()
        outcome = self._simulated_outcome(action, "outcome-model-v1")
        reward = self._simulated_reward(action, outcome, "reward-model-v2")

        with self.assertRaisesRegex(LearningEnvironmentError, "exact outcome simulation model"):
            environment.resolve(
                action.action_id,
                outcome=outcome,
                reward=reward,
                resolved_at="2026-09-17T13:05:02+00:00",
            )

    def test_simulated_reward_resolves_when_it_binds_the_outcome_model(self) -> None:
        environment, action = self._environment_and_action()
        outcome = self._simulated_outcome(action, "counterfactual-model-v1")
        reward = self._simulated_reward(action, outcome, "counterfactual-model-v1")

        transition = environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=reward,
            resolved_at="2026-09-17T13:05:02+00:00",
        )

        self.assertEqual(transition.outcome_id, outcome.outcome_id)
        self.assertEqual(transition.reward_id, reward.reward_id)


if __name__ == "__main__":
    unittest.main()
