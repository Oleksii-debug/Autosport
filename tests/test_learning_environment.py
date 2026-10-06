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
)


class CausalLearningEnvironmentTests(unittest.TestCase):
    @staticmethod
    def _identity(**overrides: object) -> EnvironmentIdentity:
        values: dict[str, object] = {
            "source_id": "paper-replay-source-v1",
            "config_id": "learning-config-v1",
            "data_id": "dataset-sha256-example",
            "protocol_id": "rq-learning-001",
            "cutoff_ts": "2026-09-17T14:00:00+00:00",
            "seed": 17,
        }
        values.update(overrides)
        return EnvironmentIdentity(**values)  # type: ignore[arg-type]

    @classmethod
    def _environment(
        cls,
        *,
        identity: EnvironmentIdentity | None = None,
        policy_id: str = "policy-transparent-v1",
    ) -> CausalLearningEnvironment:
        return CausalLearningEnvironment(
            identity or cls._identity(),
            episode_key="episode-001",
            policy_id=policy_id,
            admissible_actions=frozenset({"OBSERVE_MORE", "PAPER_PROPOSAL", "WAIT"}),
        )

    @staticmethod
    def _observation(
        environment_id: str,
        *,
        observed_at: str = "2026-09-17T13:00:00+00:00",
        available_at: str = "2026-09-17T13:00:01+00:00",
    ) -> Observation:
        return Observation(
            environment_id=environment_id,
            observed_at=observed_at,
            available_at=available_at,
            evidence=(("market_state", "snapshot-17"), ("quote", "2.10")),
        )

    @staticmethod
    def _observed_resolution(
        action: Action,
        *,
        revealed_at: str = "2026-09-17T13:05:00+00:00",
        reward_available_at: str = "2026-09-17T13:05:01+00:00",
    ) -> tuple[Outcome, RewardEvidence]:
        outcome = Outcome(
            environment_id=action.environment_id,
            action_id=action.action_id,
            revealed_at=revealed_at,
            truth=EvidenceTruth.OBSERVED,
            evidence=(("settlement", "paper-win"),),
        )
        reward = RewardEvidence(
            environment_id=action.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0.25"),
            available_at=reward_available_at,
            truth=EvidenceTruth.OBSERVED,
            evidence=(("reward_rule", "paper-return-v1"),),
        )
        return outcome, reward

    def test_environment_identity_is_seed_config_data_protocol_and_cutoff_bound(self) -> None:
        first = self._identity()
        same = self._identity()
        changed_seed = self._identity(seed=18)
        changed_config = self._identity(config_id="learning-config-v2")

        self.assertEqual(first.environment_id, same.environment_id)
        self.assertNotEqual(first.environment_id, changed_seed.environment_id)
        self.assertNotEqual(first.environment_id, changed_config.environment_id)
        self.assertEqual(len(first.environment_id), 64)

    def test_future_observation_is_rejected_at_decision_boundary(self) -> None:
        environment = self._environment()
        observation = self._observation(
            environment.environment_id,
            available_at="2026-09-17T13:10:00+00:00",
        )

        with self.assertRaisesRegex(LearningEnvironmentError, "future observation"):
            environment.act(
                observation,
                action_type="WAIT",
                decision_at="2026-09-17T13:09:59+00:00",
            )

    def test_policy_cannot_choose_outside_externally_admissible_action_set(self) -> None:
        environment = self._environment()
        observation = self._observation(environment.environment_id)

        with self.assertRaisesRegex(LearningEnvironmentError, "admissible"):
            environment.act(
                observation,
                action_type="REAL_MONEY_BET",
                decision_at="2026-09-17T13:00:02+00:00",
            )

    def test_pending_retry_collapses_by_decision_intent_and_conflict_fails_closed(self) -> None:
        environment = self._environment()
        observation = self._observation(environment.environment_id)
        first = environment.act(
            observation,
            action_type="PAPER_PROPOSAL",
            decision_at="2026-09-17T13:00:02+00:00",
            parameters=(("candidate_id", "paper-candidate-1"),),
        )

        retry = environment.act(
            observation,
            action_type="PAPER_PROPOSAL",
            decision_at="2026-09-17T13:00:03+00:00",
            parameters=(("candidate_id", "paper-candidate-1"),),
        )
        self.assertEqual(retry.action_id, first.action_id)
        self.assertEqual(retry.decided_at, first.decided_at)

        with self.assertRaisesRegex(LearningEnvironmentError, "decision intent conflicts"):
            environment.act(
                observation,
                action_type="PAPER_PROPOSAL",
                decision_at="2026-09-17T13:00:04+00:00",
                parameters=(("candidate_id", "paper-candidate-2"),),
            )

    def test_observed_transition_checkpoint_resume_and_duplicate_action_fail_closed(self) -> None:
        environment = self._environment()
        observation = self._observation(environment.environment_id)
        action = environment.act(
            observation,
            action_type="PAPER_PROPOSAL",
            decision_at="2026-09-17T13:00:02+00:00",
            parameters=(("candidate_id", "paper-candidate-1"),),
        )
        outcome, reward = self._observed_resolution(action)

        transition = environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=reward,
            resolved_at="2026-09-17T13:05:02+00:00",
        )
        checkpoint = environment.checkpoint()
        resumed = CausalLearningEnvironment.resume(
            environment.identity,
            episode_key="episode-001",
            policy_id="policy-transparent-v1",
            admissible_actions=frozenset({"OBSERVE_MORE", "PAPER_PROPOSAL", "WAIT"}),
            checkpoint=checkpoint,
        )

        self.assertEqual(checkpoint.step_index, 1)
        self.assertEqual(checkpoint.last_transition_id, transition.transition_id)
        self.assertIn(action.action_id, checkpoint.committed_action_ids)
        self.assertEqual(len(checkpoint.committed_decision_intents), 1)
        self.assertEqual(resumed.checkpoint(), checkpoint)
        with self.assertRaisesRegex(LearningEnvironmentError, "already committed"):
            resumed.act(
                observation,
                action_type="PAPER_PROPOSAL",
                decision_at="2026-09-17T13:00:03+00:00",
                parameters=(("candidate_id", "paper-candidate-1"),),
            )
        with self.assertRaisesRegex(LearningEnvironmentError, "decision intent conflicts"):
            resumed.act(
                observation,
                action_type="PAPER_PROPOSAL",
                decision_at="2026-09-17T13:00:03+00:00",
                parameters=(("candidate_id", "paper-candidate-2"),),
            )

    def test_checkpoint_refuses_to_erase_unresolved_action(self) -> None:
        environment = self._environment()
        observation = self._observation(environment.environment_id)
        environment.act(
            observation,
            action_type="WAIT",
            decision_at="2026-09-17T13:00:02+00:00",
        )

        with self.assertRaisesRegex(LearningEnvironmentError, "unresolved actions"):
            environment.checkpoint()

    def test_observed_and_simulated_truth_cannot_be_relabelled(self) -> None:
        environment = self._environment()
        observation = self._observation(environment.environment_id)
        action = environment.act(
            observation,
            action_type="OBSERVE_MORE",
            decision_at="2026-09-17T13:00:02+00:00",
        )
        outcome = Outcome(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            revealed_at="2026-09-17T13:01:00+00:00",
            truth=EvidenceTruth.SIMULATED,
            evidence=(("counterfactual", "modelled-no-action"),),
            simulation_model_id="counterfactual-model-v1",
        )
        observed_reward = RewardEvidence(
            environment_id=environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=Decimal("0"),
            available_at="2026-09-17T13:01:01+00:00",
            truth=EvidenceTruth.OBSERVED,
            evidence=(("reward_rule", "zero"),),
        )

        with self.assertRaisesRegex(LearningEnvironmentError, "truth label conflicts"):
            environment.resolve(
                action.action_id,
                outcome=outcome,
                reward=observed_reward,
                resolved_at="2026-09-17T13:01:02+00:00",
            )

    def test_simulated_evidence_requires_explicit_model_identity(self) -> None:
        environment = self._environment()
        observation = self._observation(environment.environment_id)
        action = environment.act(
            observation,
            action_type="WAIT",
            decision_at="2026-09-17T13:00:02+00:00",
        )

        with self.assertRaisesRegex(LearningEnvironmentError, "simulation_model_id"):
            Outcome(
                environment_id=environment.environment_id,
                action_id=action.action_id,
                revealed_at="2026-09-17T13:01:00+00:00",
                truth=EvidenceTruth.SIMULATED,
                evidence=(("counterfactual", "synthetic"),),
            )

    def test_outcome_or_reward_before_decision_is_future_leakage_conflict(self) -> None:
        environment = self._environment()
        observation = self._observation(environment.environment_id)
        action = environment.act(
            observation,
            action_type="WAIT",
            decision_at="2026-09-17T13:00:02+00:00",
        )
        outcome, reward = self._observed_resolution(
            action,
            revealed_at="2026-09-17T13:00:01+00:00",
            reward_available_at="2026-09-17T13:05:01+00:00",
        )

        with self.assertRaisesRegex(LearningEnvironmentError, "leaks before"):
            environment.resolve(
                action.action_id,
                outcome=outcome,
                reward=reward,
                resolved_at="2026-09-17T13:05:02+00:00",
            )

    def test_resume_rejects_seed_or_policy_rebinding(self) -> None:
        environment = self._environment()
        checkpoint = environment.checkpoint()

        with self.assertRaisesRegex(LearningEnvironmentError, "environment identity mismatch"):
            CausalLearningEnvironment.resume(
                self._identity(seed=99),
                episode_key="episode-001",
                policy_id="policy-transparent-v1",
                admissible_actions=frozenset({"OBSERVE_MORE", "PAPER_PROPOSAL", "WAIT"}),
                checkpoint=checkpoint,
            )
        with self.assertRaisesRegex(LearningEnvironmentError, "episode identity mismatch"):
            CausalLearningEnvironment.resume(
                environment.identity,
                episode_key="episode-001",
                policy_id="policy-transparent-v2",
                admissible_actions=frozenset({"OBSERVE_MORE", "PAPER_PROPOSAL", "WAIT"}),
                checkpoint=checkpoint,
            )


if __name__ == "__main__":
    unittest.main()
