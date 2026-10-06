import unittest
from decimal import Decimal

from autosport.learning_environment import (
    Action,
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    LearningEnvironmentError,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)


class LearningEnvironmentTimestampIdentityTests(unittest.TestCase):
    @staticmethod
    def _identity(cutoff_ts: str) -> EnvironmentIdentity:
        return EnvironmentIdentity(
            source_id="paper-replay-source-v1",
            config_id="learning-config-v1",
            data_id="dataset-sha256-example",
            protocol_id="rq-learning-001",
            cutoff_ts=cutoff_ts,
            seed=17,
        )

    @staticmethod
    def _observation(environment_id: str, observed_at: str, available_at: str) -> Observation:
        return Observation(
            environment_id=environment_id,
            observed_at=observed_at,
            available_at=available_at,
            evidence=(("market_state", "snapshot-17"), ("quote", "2.10")),
        )

    def test_timezone_equivalent_instants_have_identical_causal_ids(self) -> None:
        utc_identity = self._identity("2026-09-17T14:00:00Z")
        offset_identity = self._identity("2026-09-17T16:00:00+02:00")
        self.assertEqual(utc_identity.environment_id, offset_identity.environment_id)

        utc_observation = self._observation(
            utc_identity.environment_id,
            "2026-09-17T13:00:00Z",
            "2026-09-17T13:00:01Z",
        )
        offset_observation = self._observation(
            offset_identity.environment_id,
            "2026-09-17T15:00:00+02:00",
            "2026-09-17T15:00:01+02:00",
        )
        self.assertEqual(utc_observation.observation_id, offset_observation.observation_id)

        utc_action = Action(
            environment_id=utc_identity.environment_id,
            observation_id=utc_observation.observation_id,
            action_type="PAPER_PROPOSAL",
            decided_at="2026-09-17T13:00:02Z",
            parameters=(("candidate_id", "paper-candidate-1"),),
        )
        offset_action = Action(
            environment_id=offset_identity.environment_id,
            observation_id=offset_observation.observation_id,
            action_type="PAPER_PROPOSAL",
            decided_at="2026-09-17T15:00:02+02:00",
            parameters=(("candidate_id", "paper-candidate-1"),),
        )
        self.assertEqual(utc_action.action_id, offset_action.action_id)

        utc_outcome = Outcome(
            environment_id=utc_identity.environment_id,
            action_id=utc_action.action_id,
            revealed_at="2026-09-17T13:05:00Z",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("settlement", "paper-win"),),
        )
        offset_outcome = Outcome(
            environment_id=offset_identity.environment_id,
            action_id=offset_action.action_id,
            revealed_at="2026-09-17T15:05:00+02:00",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("settlement", "paper-win"),),
        )
        self.assertEqual(utc_outcome.outcome_id, offset_outcome.outcome_id)

        utc_reward = RewardEvidence(
            environment_id=utc_identity.environment_id,
            action_id=utc_action.action_id,
            outcome_id=utc_outcome.outcome_id,
            reward=Decimal("0.25"),
            available_at="2026-09-17T13:05:01Z",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("reward_rule", "paper-return-v1"),),
        )
        offset_reward = RewardEvidence(
            environment_id=offset_identity.environment_id,
            action_id=offset_action.action_id,
            outcome_id=offset_outcome.outcome_id,
            reward=Decimal("0.25"),
            available_at="2026-09-17T15:05:01+02:00",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("reward_rule", "paper-return-v1"),),
        )
        self.assertEqual(utc_reward.reward_id, offset_reward.reward_id)

        utc_transition = Transition(
            environment_id=utc_identity.environment_id,
            episode_id="1" * 64,
            step_index=1,
            observation_id=utc_observation.observation_id,
            action_id=utc_action.action_id,
            outcome_id=utc_outcome.outcome_id,
            reward_id=utc_reward.reward_id,
            decision_at="2026-09-17T13:00:02Z",
            resolved_at="2026-09-17T13:05:02Z",
        )
        offset_transition = Transition(
            environment_id=offset_identity.environment_id,
            episode_id="1" * 64,
            step_index=1,
            observation_id=offset_observation.observation_id,
            action_id=offset_action.action_id,
            outcome_id=offset_outcome.outcome_id,
            reward_id=offset_reward.reward_id,
            decision_at="2026-09-17T15:00:02+02:00",
            resolved_at="2026-09-17T15:05:02+02:00",
        )
        self.assertEqual(utc_transition.transition_id, offset_transition.transition_id)

    def test_restart_duplicate_action_rejects_timezone_alias(self) -> None:
        identity = self._identity("2026-09-17T14:00:00Z")
        environment = CausalLearningEnvironment(
            identity,
            episode_key="episode-001",
            policy_id="policy-transparent-v1",
            admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
        )
        observation = self._observation(
            environment.environment_id,
            "2026-09-17T13:00:00Z",
            "2026-09-17T13:00:01Z",
        )
        action = environment.act(
            observation,
            action_type="PAPER_PROPOSAL",
            decision_at="2026-09-17T13:00:02Z",
            parameters=(("candidate_id", "paper-candidate-1"),),
        )
        outcome = Outcome(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-17T13:05:00Z",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("settlement", "paper-win"),),
        )
        reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0.25"),
            available_at="2026-09-17T13:05:01Z",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("reward_rule", "paper-return-v1"),),
        )
        environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=reward,
            resolved_at="2026-09-17T13:05:02Z",
        )
        checkpoint = environment.checkpoint()
        resumed = CausalLearningEnvironment.resume(
            self._identity("2026-09-17T16:00:00+02:00"),
            episode_key="episode-001",
            policy_id="policy-transparent-v1",
            admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
            checkpoint=checkpoint,
        )
        equivalent_observation = self._observation(
            resumed.environment_id,
            "2026-09-17T15:00:00+02:00",
            "2026-09-17T15:00:01+02:00",
        )

        with self.assertRaisesRegex(LearningEnvironmentError, "already committed"):
            resumed.act(
                equivalent_observation,
                action_type="PAPER_PROPOSAL",
                decision_at="2026-09-17T15:00:02+02:00",
                parameters=(("candidate_id", "paper-candidate-1"),),
            )

    def test_observation_cannot_cross_environment_cutoff(self) -> None:
        identity = self._identity("2026-09-17T14:00:00Z")
        environment = CausalLearningEnvironment(
            identity,
            episode_key="episode-001",
            policy_id="policy-transparent-v1",
            admissible_actions=frozenset({"WAIT"}),
        )

        cases = (
            ("2026-09-17T14:00:01Z", "2026-09-17T14:00:02Z"),
            ("2026-09-17T13:59:59Z", "2026-09-17T14:00:01Z"),
            ("2026-09-17T16:00:01+02:00", "2026-09-17T16:00:02+02:00"),
        )
        for observed_at, available_at in cases:
            with self.subTest(observed_at=observed_at, available_at=available_at):
                observation = self._observation(
                    environment.environment_id,
                    observed_at,
                    available_at,
                )
                with self.assertRaisesRegex(LearningEnvironmentError, "cutoff"):
                    environment.act(
                        observation,
                        action_type="WAIT",
                        decision_at="2026-09-17T14:00:03Z",
                    )


if __name__ == "__main__":
    unittest.main()
