import hashlib
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
from autosport.transparent_bandit_policy import BanditPolicyState


class TransparentBanditPolicyTests(unittest.TestCase):
    def _resolved_paper_step(self):
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
            episode_key="episode-policy-update",
            policy_id="transparent-bandit-bootstrap-v1",
            admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
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
        config_sha256 = hashlib.sha256(b"transparent-bandit-config-v1").hexdigest()
        policy = BanditPolicyState.initial(
            environment_id=environment.environment_id,
            protocol_id="rq-learning-001",
            config_sha256=config_sha256,
            seed=17,
            action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
        )
        return policy, action, reward, transition

    def test_update_is_exact_deterministic_and_preserves_simulated_truth(self) -> None:
        policy, action, reward, transition = self._resolved_paper_step()

        successor, evidence = policy.update(
            action=action,
            reward=reward,
            transition=transition,
        )
        repeat_successor, repeat_evidence = policy.update(
            action=action,
            reward=reward,
            transition=transition,
        )

        self.assertEqual(successor, repeat_successor)
        self.assertEqual(evidence, repeat_evidence)
        self.assertEqual(successor.predecessor_policy_id, policy.policy_id)
        self.assertEqual(evidence.successor_policy_id, successor.policy_id)
        self.assertEqual(evidence.reward_id, reward.reward_id)
        self.assertIs(evidence.reward_truth, EvidenceTruth.SIMULATED)
        self.assertEqual(evidence.simulation_model_id, "paper-fill-return-model-v1")
        self.assertEqual(len(evidence.update_id), 64)

    def test_successor_rejects_duplicate_or_revised_reward_for_same_action(self) -> None:
        policy, action, reward, transition = self._resolved_paper_step()
        successor, _ = policy.update(action=action, reward=reward, transition=transition)

        with self.assertRaisesRegex(LearningEnvironmentError, "already has"):
            successor.update(action=action, reward=reward, transition=transition)

        revised_reward = RewardEvidence(
            environment_id=reward.environment_id,
            action_id=reward.action_id,
            outcome_id=reward.outcome_id,
            reward=Decimal("0.30"),
            available_at=reward.available_at,
            truth=reward.truth,
            evidence=(("paper_execution", "revised-fill"),),
            simulation_model_id=reward.simulation_model_id,
        )
        with self.assertRaisesRegex(LearningEnvironmentError, "exact resolved reward"):
            policy.update(
                action=action,
                reward=revised_reward,
                transition=transition,
            )

    def test_policy_can_only_choose_within_external_admissible_subset(self) -> None:
        policy, action, reward, transition = self._resolved_paper_step()
        successor, _ = policy.update(action=action, reward=reward, transition=transition)

        self.assertEqual(
            successor.choose(admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"})),
            "PAPER_PROPOSAL",
        )
        self.assertEqual(
            successor.choose(admissible_actions=frozenset({"WAIT"})),
            "WAIT",
        )
        with self.assertRaisesRegex(LearningEnvironmentError, "outside immutable policy identity"):
            successor.choose(admissible_actions=frozenset({"REAL_MONEY_BET"}))

    def test_seed_or_config_rebinding_changes_policy_identity(self) -> None:
        policy, _, _, _ = self._resolved_paper_step()
        changed_seed = BanditPolicyState.initial(
            environment_id=policy.environment_id,
            protocol_id=policy.protocol_id,
            config_sha256=policy.config_sha256,
            seed=18,
            action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
        )
        changed_config = BanditPolicyState.initial(
            environment_id=policy.environment_id,
            protocol_id=policy.protocol_id,
            config_sha256=hashlib.sha256(b"transparent-bandit-config-v2").hexdigest(),
            seed=17,
            action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
        )

        self.assertNotEqual(policy.policy_id, changed_seed.policy_id)
        self.assertNotEqual(policy.policy_id, changed_config.policy_id)


if __name__ == "__main__":
    unittest.main()
