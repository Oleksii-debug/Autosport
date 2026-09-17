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


class LearningEnvironmentIdentityCanonicalizationTests(unittest.TestCase):
    @staticmethod
    def _identity(cutoff_ts: str = "2026-09-17T14:00:00Z") -> EnvironmentIdentity:
        return EnvironmentIdentity(
            source_id="paper-replay-source-v1",
            config_id="learning-config-v1",
            data_id="dataset-sha256-example",
            protocol_id="rq-learning-001",
            cutoff_ts=cutoff_ts,
            seed=17,
        )

    @staticmethod
    def _environment(identity: EnvironmentIdentity) -> CausalLearningEnvironment:
        return CausalLearningEnvironment(
            identity,
            episode_key="episode-time-identity",
            policy_id="policy-transparent-v1",
            admissible_actions=frozenset({"WAIT"}),
        )

    @staticmethod
    def _observation(
        environment_id: str,
        *,
        observed_at: str,
        available_at: str,
    ) -> Observation:
        return Observation(
            environment_id=environment_id,
            observed_at=observed_at,
            available_at=available_at,
            evidence=(("market_state", "snapshot-17"),),
        )

    def test_timezone_equivalent_instants_have_same_identity_across_contracts(self) -> None:
        identity_utc = self._identity("2026-09-17T14:00:00Z")
        identity_offset = self._identity("2026-09-17T16:00:00+02:00")
        self.assertEqual(identity_utc.environment_id, identity_offset.environment_id)

        observation_utc = self._observation(
            identity_utc.environment_id,
            observed_at="2026-09-17T13:00:00Z",
            available_at="2026-09-17T13:00:01Z",
        )
        observation_offset = self._observation(
            identity_utc.environment_id,
            observed_at="2026-09-17T15:00:00+02:00",
            available_at="2026-09-17T15:00:01+02:00",
        )
        self.assertEqual(observation_utc.observation_id, observation_offset.observation_id)

        action_utc = Action(
            environment_id=identity_utc.environment_id,
            observation_id=observation_utc.observation_id,
            action_type="WAIT",
            decided_at="2026-09-17T13:00:02Z",
        )
        action_offset = Action(
            environment_id=identity_utc.environment_id,
            observation_id=observation_offset.observation_id,
            action_type="WAIT",
            decided_at="2026-09-17T15:00:02+02:00",
        )
        self.assertEqual(action_utc.action_id, action_offset.action_id)

        outcome_utc = Outcome(
            environment_id=identity_utc.environment_id,
            action_id=action_utc.action_id,
            revealed_at="2026-09-17T13:05:00Z",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("settlement", "paper-noop"),),
        )
        outcome_offset = Outcome(
            environment_id=identity_utc.environment_id,
            action_id=action_offset.action_id,
            revealed_at="2026-09-17T15:05:00+02:00",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("settlement", "paper-noop"),),
        )
        self.assertEqual(outcome_utc.outcome_id, outcome_offset.outcome_id)

        reward_utc = RewardEvidence(
            environment_id=identity_utc.environment_id,
            action_id=action_utc.action_id,
            outcome_id=outcome_utc.outcome_id,
            reward=Decimal("0"),
            available_at="2026-09-17T13:05:01Z",
            truth=EvidenceTruth.OBSERVED,
        )
        reward_offset = RewardEvidence(
            environment_id=identity_utc.environment_id,
            action_id=action_offset.action_id,
            outcome_id=outcome_offset.outcome_id,
            reward=Decimal("0"),
            available_at="2026-09-17T15:05:01+02:00",
            truth=EvidenceTruth.OBSERVED,
        )
        self.assertEqual(reward_utc.reward_id, reward_offset.reward_id)

        environment = self._environment(identity_utc)
        transition_utc = Transition(
            environment_id=identity_utc.environment_id,
            episode_id=environment.episode.episode_id,
            step_index=1,
            observation_id=observation_utc.observation_id,
            action_id=action_utc.action_id,
            outcome_id=outcome_utc.outcome_id,
            reward_id=reward_utc.reward_id,
            decision_at="2026-09-17T13:00:02Z",
            resolved_at="2026-09-17T13:05:02Z",
        )
        transition_offset = Transition(
            environment_id=identity_utc.environment_id,
            episode_id=environment.episode.episode_id,
            step_index=1,
            observation_id=observation_offset.observation_id,
            action_id=action_offset.action_id,
            outcome_id=outcome_offset.outcome_id,
            reward_id=reward_offset.reward_id,
            decision_at="2026-09-17T15:00:02+02:00",
            resolved_at="2026-09-17T15:05:02+02:00",
        )
        self.assertEqual(transition_utc.transition_id, transition_offset.transition_id)

    def test_restart_rejects_timezone_alias_of_committed_action(self) -> None:
        identity = self._identity()
        environment = self._environment(identity)
        observation = self._observation(
            environment.environment_id,
            observed_at="2026-09-17T13:00:00Z",
            available_at="2026-09-17T13:00:01Z",
        )
        action = environment.act(
            observation,
            action_type="WAIT",
            decision_at="2026-09-17T13:00:02Z",
        )
        outcome = Outcome(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-17T13:05:00Z",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("settlement", "paper-noop"),),
        )
        reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0"),
            available_at="2026-09-17T13:05:01Z",
            truth=EvidenceTruth.OBSERVED,
        )
        environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=reward,
            resolved_at="2026-09-17T13:05:02Z",
        )
        resumed = CausalLearningEnvironment.resume(
            identity,
            episode_key="episode-time-identity",
            policy_id="policy-transparent-v1",
            admissible_actions=frozenset({"WAIT"}),
            checkpoint=environment.checkpoint(),
        )

        with self.assertRaisesRegex(LearningEnvironmentError, "already committed"):
            resumed.act(
                observation,
                action_type="WAIT",
                decision_at="2026-09-17T15:00:02+02:00",
            )

    def test_cutoff_blocks_late_observation_but_not_delayed_resolution(self) -> None:
        identity = self._identity()
        environment = self._environment(identity)
        late_observation = self._observation(
            environment.environment_id,
            observed_at="2026-09-17T14:00:00Z",
            available_at="2026-09-17T14:00:01Z",
        )
        with self.assertRaisesRegex(LearningEnvironmentError, "evidence cutoff"):
            environment.act(
                late_observation,
                action_type="WAIT",
                decision_at="2026-09-17T14:00:02Z",
            )

        pre_cutoff_observation = self._observation(
            environment.environment_id,
            observed_at="2026-09-17T13:59:57Z",
            available_at="2026-09-17T13:59:58Z",
        )
        action = environment.act(
            pre_cutoff_observation,
            action_type="WAIT",
            decision_at="2026-09-17T13:59:59Z",
        )
        outcome = Outcome(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-17T14:05:00Z",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("settlement", "paper-noop"),),
        )
        reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0"),
            available_at="2026-09-17T14:05:01Z",
            truth=EvidenceTruth.OBSERVED,
        )
        transition = environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=reward,
            resolved_at="2026-09-17T14:05:02Z",
        )
        self.assertEqual(transition.action_id, action.action_id)


if __name__ == "__main__":
    unittest.main()
