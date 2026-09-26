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


class TransparentBanditHistoryReplayIntegrityTests(unittest.TestCase):
    def _resolved_step(self):
        identity = EnvironmentIdentity(
            source_id="paper-replay-source-v1",
            config_id="learning-config-v1",
            data_id="dataset-sha256-history-replay",
            protocol_id="rq-learning-history-001",
            cutoff_ts="2026-09-23T00:00:00+00:00",
            seed=23,
        )
        environment = CausalLearningEnvironment(
            identity,
            episode_key="episode-history-replay",
            policy_id="transparent-bandit-bootstrap-v1",
            admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
        )
        observation = Observation(
            environment_id=environment.environment_id,
            observed_at="2026-09-22T23:59:50+00:00",
            available_at="2026-09-22T23:59:51+00:00",
            evidence=(("quote", "2.10"),),
        )
        action = environment.act(
            observation,
            action_type="PAPER_PROPOSAL",
            decision_at="2026-09-22T23:59:52+00:00",
        )
        outcome = Outcome(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-23T00:00:10+00:00",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("match_result", "home-win"),),
        )
        reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0.25"),
            available_at="2026-09-23T00:00:11+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("paper_execution", "modelled-fill"),),
            simulation_model_id="paper-fill-return-model-v1",
        )
        transition = environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=reward,
            resolved_at="2026-09-23T00:00:12+00:00",
        )
        policy = BanditPolicyState.initial(
            environment_id=environment.environment_id,
            protocol_id="rq-learning-history-001",
            config_sha256=hashlib.sha256(b"history-replay-config-v1").hexdigest(),
            seed=23,
            action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
        )
        return policy, action, reward, transition

    def test_restart_rejects_substituted_applied_history_that_reopens_replay(self) -> None:
        policy, action, reward, transition = self._resolved_step()
        successor, _ = policy.update(
            action=action,
            reward=reward,
            transition=transition,
        )
        payload = successor.to_payload()
        self.assertEqual(payload["applied_action_ids"], [action.action_id])
        self.assertEqual(payload["applied_reward_ids"], [reward.reward_id])

        # An attacker who can rewrite a restart artifact can currently replace both
        # replay fences with unrelated, syntactically valid SHA-256 identities while
        # leaving the already-counted estimate intact.  Re-hashing policy_id cannot
        # reveal that the history no longer proves the accumulated reward.
        payload["applied_action_ids"] = ["1" * 64]
        payload["applied_reward_ids"] = ["2" * 64]

        with self.assertRaisesRegex(
            LearningEnvironmentError,
            "history|integrity|provenance|replay",
        ):
            BanditPolicyState.from_payload(payload)

    def test_genuine_successor_payload_remains_restartable_and_replay_safe(self) -> None:
        policy, action, reward, transition = self._resolved_step()
        successor, _ = policy.update(
            action=action,
            reward=reward,
            transition=transition,
        )

        restarted = BanditPolicyState.from_payload(successor.to_payload())
        self.assertEqual(restarted, successor)
        with self.assertRaisesRegex(LearningEnvironmentError, "already has"):
            restarted.update(action=action, reward=reward, transition=transition)


if __name__ == "__main__":
    unittest.main()
