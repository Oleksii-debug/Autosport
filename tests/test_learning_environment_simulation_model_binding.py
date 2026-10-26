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
    def _pending_action(self) -> tuple[CausalLearningEnvironment, object]:
        identity = EnvironmentIdentity(
            source_id="paper-simulator-source-v1",
            config_id="learning-config-v1",
            data_id="dataset-snapshot-v1",
            protocol_id="reward-hacking-protocol-v1",
            cutoff_ts="2026-09-21T10:00:00+00:00",
            seed=21,
        )
        environment = CausalLearningEnvironment(
            identity,
            episode_key="episode-simulation-model-binding",
            policy_id="policy-shadow-v1",
            admissible_actions=frozenset({"WAIT"}),
        )
        observation = Observation(
            environment_id=environment.environment_id,
            observed_at="2026-09-21T09:01:00+00:00",
            available_at="2026-09-21T09:01:01+00:00",
            evidence=(("market_state", "snapshot-21"),),
        )
        action = environment.act(
            observation,
            action_type="WAIT",
            decision_at="2026-09-21T09:01:02+00:00",
        )
        return environment, action

    def test_simulated_reward_must_bind_exact_outcome_simulation_model(self) -> None:
        environment, action = self._pending_action()
        outcome = Outcome(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-21T09:02:00+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("counterfactual", "outcome"),),
            simulation_model_id="simulator-a-v1",
        )
        mismatched_reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0.125"),
            available_at="2026-09-21T09:02:01+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("reward_rule", "counterfactual-return-v1"),),
            simulation_model_id="simulator-b-v1",
        )

        with self.assertRaisesRegex(
            LearningEnvironmentError,
            "exact outcome simulation_model_id",
        ):
            environment.resolve(
                action.action_id,
                outcome=outcome,
                reward=mismatched_reward,
                resolved_at="2026-09-21T09:02:02+00:00",
            )

        matching_reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0.125"),
            available_at="2026-09-21T09:02:01+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("reward_rule", "counterfactual-return-v1"),),
            simulation_model_id="simulator-a-v1",
        )
        transition = environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=matching_reward,
            resolved_at="2026-09-21T09:02:02+00:00",
        )

        self.assertEqual(transition.action_id, action.action_id)
        self.assertEqual(transition.outcome_id, outcome.outcome_id)
        self.assertEqual(transition.reward_id, matching_reward.reward_id)


    def test_simulated_outcome_rejects_observed_reward_without_consuming_action(self) -> None:
        environment, action = self._pending_action()
        outcome = Outcome(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-21T09:02:00+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("counterfactual", "outcome"),),
            simulation_model_id="simulator-a-v1",
        )
        mismatched_reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0.125"),
            available_at="2026-09-21T09:02:01+00:00",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("reward_rule", "observed-return-v1"),),
        )

        with self.assertRaisesRegex(
            LearningEnvironmentError,
            "reward truth label conflicts",
        ):
            environment.resolve(
                action.action_id,
                outcome=outcome,
                reward=mismatched_reward,
                resolved_at="2026-09-21T09:02:02+00:00",
            )

        matching_reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0.125"),
            available_at="2026-09-21T09:02:01+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("reward_rule", "counterfactual-return-v1"),),
            simulation_model_id="simulator-a-v1",
        )
        transition = environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=matching_reward,
            resolved_at="2026-09-21T09:02:02+00:00",
        )

        self.assertEqual(transition.action_id, action.action_id)
        self.assertEqual(transition.outcome_id, outcome.outcome_id)
        self.assertEqual(transition.reward_id, matching_reward.reward_id)

    def test_observed_outcome_can_feed_model_bound_simulated_reward(self) -> None:
        environment, action = self._pending_action()
        outcome = Outcome(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-21T09:02:00+00:00",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("settled_result", "observed"),),
        )
        simulated_reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0.125"),
            available_at="2026-09-21T09:02:01+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("reward_rule", "paper-fill-return-v1"),),
            simulation_model_id="paper-fill-return-model-v1",
        )

        transition = environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=simulated_reward,
            resolved_at="2026-09-21T09:02:02+00:00",
        )

        self.assertEqual(transition.action_id, action.action_id)
        self.assertEqual(transition.outcome_id, outcome.outcome_id)
        self.assertEqual(transition.reward_id, simulated_reward.reward_id)


if __name__ == "__main__":
    unittest.main()
