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

    def test_learning_identity_scalar_aliases_fail_closed(self) -> None:
        class IntAlias(int):
            pass

        class FrozenSetAlias(frozenset):
            pass

        with self.assertRaisesRegex(
            LearningEnvironmentError, "unsupported learning environment schema version"
        ):
            self._identity(schema_version=True)
        with self.assertRaisesRegex(LearningEnvironmentError, "exact integer"):
            self._identity(seed=IntAlias(17))
        with self.assertRaisesRegex(LearningEnvironmentError, "frozenset"):
            CausalLearningEnvironment(
                self._identity(),
                episode_key="episode-001",
                policy_id="policy-transparent-v1",
                admissible_actions=FrozenSetAlias(
                    {"OBSERVE_MORE", "PAPER_PROPOSAL", "WAIT"}
                ),
            )

        environment = self._environment()
        observation = self._observation(environment.environment_id)
        action = environment.act(
            observation,
            action_type="WAIT",
            decision_at="2026-09-17T13:00:02+00:00",
        )
        outcome, reward = self._observed_resolution(action)
        transition = environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=reward,
            resolved_at="2026-09-17T13:05:02+00:00",
        )
        transition_type = type(transition)
        with self.assertRaisesRegex(LearningEnvironmentError, "step_index must be an exact integer"):
            transition_type(
                environment_id=transition.environment_id,
                episode_id=transition.episode_id,
                step_index=IntAlias(transition.step_index),
                observation_id=transition.observation_id,
                action_id=transition.action_id,
                outcome_id=transition.outcome_id,
                reward_id=transition.reward_id,
                decision_at=transition.decision_at,
                resolved_at=transition.resolved_at,
            )

        checkpoint = environment.checkpoint()
        checkpoint_type = type(checkpoint)
        with self.assertRaisesRegex(
            LearningEnvironmentError, "checkpoint step_index must be an exact integer"
        ):
            checkpoint_type(
                environment_id=checkpoint.environment_id,
                episode_id=checkpoint.episode_id,
                policy_id=checkpoint.policy_id,
                step_index=IntAlias(checkpoint.step_index),
                chain_sha256=checkpoint.chain_sha256,
                last_transition_id=checkpoint.last_transition_id,
                committed_action_ids=checkpoint.committed_action_ids,
                committed_decision_intents=checkpoint.committed_decision_intents,
            )

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

    def test_decision_boundary_rejects_observation_subclass_before_identity_dispatch(self) -> None:
        class HostileObservation(Observation):
            __slots__ = ()

            @property
            def observation_id(self) -> str:
                raise AssertionError("Observation subclass identity dispatch must not execute")

        environment = self._environment()
        exact = self._observation(environment.environment_id)
        hostile = HostileObservation(
            environment_id=exact.environment_id,
            observed_at=exact.observed_at,
            available_at=exact.available_at,
            evidence=exact.evidence,
        )

        with self.assertRaisesRegex(TypeError, "exact Observation"):
            environment.act(
                hostile,
                action_type="WAIT",
                decision_at="2026-09-17T13:00:02+00:00",
            )



    def test_resolution_boundary_rejects_evidence_subclasses_before_causal_dispatch(self) -> None:
        class HostileOutcome(Outcome):
            __slots__ = ("_armed",)

            def __post_init__(self) -> None:
                object.__setattr__(self, "_armed", False)
                super().__post_init__()
                object.__setattr__(self, "_armed", True)

            def __getattribute__(self, name: str):
                if name in {"_armed", "__class__", "__dict__"}:
                    return object.__getattribute__(self, name)
                try:
                    armed = object.__getattribute__(self, "_armed")
                except AttributeError:
                    armed = False
                if armed:
                    raise AssertionError(
                        f"Outcome subclass member dispatch must not execute: {name}"
                    )
                return object.__getattribute__(self, name)

        class HostileReward(RewardEvidence):
            __slots__ = ("_armed",)

            def __post_init__(self) -> None:
                object.__setattr__(self, "_armed", False)
                super().__post_init__()
                object.__setattr__(self, "_armed", True)

            def __getattribute__(self, name: str):
                if name in {"_armed", "__class__", "__dict__"}:
                    return object.__getattribute__(self, name)
                try:
                    armed = object.__getattribute__(self, "_armed")
                except AttributeError:
                    armed = False
                if armed:
                    raise AssertionError(
                        f"RewardEvidence subclass member dispatch must not execute: {name}"
                    )
                return object.__getattribute__(self, name)

        environment = self._environment()
        observation = self._observation(environment.environment_id)
        action = environment.act(
            observation,
            action_type="WAIT",
            decision_at="2026-09-17T13:00:02+00:00",
        )
        outcome, reward = self._observed_resolution(action)
        hostile_outcome = HostileOutcome(
            environment_id=outcome.environment_id,
            action_id=outcome.action_id,
            revealed_at=outcome.revealed_at,
            truth=outcome.truth,
            evidence=outcome.evidence,
            simulation_model_id=outcome.simulation_model_id,
        )
        hostile_reward = HostileReward(
            environment_id=reward.environment_id,
            action_id=reward.action_id,
            outcome_id=reward.outcome_id,
            reward=reward.reward,
            available_at=reward.available_at,
            truth=reward.truth,
            evidence=reward.evidence,
            simulation_model_id=reward.simulation_model_id,
        )

        with self.assertRaisesRegex(TypeError, "exact canonical environment evidence"):
            environment.resolve(
                action.action_id,
                outcome=hostile_outcome,
                reward=reward,
                resolved_at="2026-09-17T13:05:02+00:00",
            )
        with self.assertRaisesRegex(TypeError, "exact canonical environment evidence"):
            environment.resolve(
                action.action_id,
                outcome=outcome,
                reward=hostile_reward,
                resolved_at="2026-09-17T13:05:02+00:00",
            )


    def test_learning_identity_and_restart_boundaries_reject_record_subclasses(self) -> None:
        class IdentitySubclass(EnvironmentIdentity):
            __slots__ = ()

        class ObservationSubclass(Observation):
            __slots__ = ()

        class OutcomeSubclass(Outcome):
            __slots__ = ()

        class RewardSubclass(RewardEvidence):
            __slots__ = ()

        exact_identity = self._identity()
        identity_subclass = IdentitySubclass(
            source_id=exact_identity.source_id,
            config_id=exact_identity.config_id,
            data_id=exact_identity.data_id,
            protocol_id=exact_identity.protocol_id,
            cutoff_ts=exact_identity.cutoff_ts,
            seed=exact_identity.seed,
        )
        with self.assertRaisesRegex(TypeError, "exact EnvironmentIdentity"):
            self._environment(identity=identity_subclass)

        environment = self._environment()
        exact_observation = self._observation(environment.environment_id)
        observation_subclass = ObservationSubclass(
            environment_id=exact_observation.environment_id,
            observed_at=exact_observation.observed_at,
            available_at=exact_observation.available_at,
            evidence=exact_observation.evidence,
        )
        with self.assertRaisesRegex(TypeError, "exact Observation"):
            environment.act(
                observation_subclass,
                action_type="WAIT",
                decision_at="2026-09-17T13:00:02+00:00",
            )

        action = environment.act(
            exact_observation,
            action_type="WAIT",
            decision_at="2026-09-17T13:00:02+00:00",
        )
        exact_outcome, exact_reward = self._observed_resolution(action)
        outcome_subclass = OutcomeSubclass(
            environment_id=exact_outcome.environment_id,
            action_id=exact_outcome.action_id,
            revealed_at=exact_outcome.revealed_at,
            truth=exact_outcome.truth,
            evidence=exact_outcome.evidence,
            simulation_model_id=exact_outcome.simulation_model_id,
        )
        reward_subclass = RewardSubclass(
            environment_id=exact_reward.environment_id,
            action_id=exact_reward.action_id,
            outcome_id=exact_reward.outcome_id,
            reward=exact_reward.reward,
            available_at=exact_reward.available_at,
            truth=exact_reward.truth,
            evidence=exact_reward.evidence,
            simulation_model_id=exact_reward.simulation_model_id,
        )
        with self.assertRaisesRegex(TypeError, "exact canonical environment evidence"):
            environment.resolve(
                action.action_id,
                outcome=outcome_subclass,
                reward=exact_reward,
                resolved_at="2026-09-17T13:05:02+00:00",
            )
        with self.assertRaisesRegex(TypeError, "exact canonical environment evidence"):
            environment.resolve(
                action.action_id,
                outcome=exact_outcome,
                reward=reward_subclass,
                resolved_at="2026-09-17T13:05:02+00:00",
            )

        restart_environment = self._environment()
        checkpoint = restart_environment.checkpoint()

        class CheckpointSubclass(type(checkpoint)):
            __slots__ = ()

        checkpoint_subclass = CheckpointSubclass(
            environment_id=checkpoint.environment_id,
            episode_id=checkpoint.episode_id,
            policy_id=checkpoint.policy_id,
            step_index=checkpoint.step_index,
            chain_sha256=checkpoint.chain_sha256,
            last_transition_id=checkpoint.last_transition_id,
            committed_action_ids=checkpoint.committed_action_ids,
            committed_decision_intents=checkpoint.committed_decision_intents,
        )
        with self.assertRaisesRegex(TypeError, "exact EnvironmentCheckpoint"):
            CausalLearningEnvironment.resume(
                restart_environment.identity,
                episode_key="episode-001",
                policy_id="policy-transparent-v1",
                admissible_actions=frozenset(
                    {"OBSERVE_MORE", "PAPER_PROPOSAL", "WAIT"}
                ),
                checkpoint=checkpoint_subclass,
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


def test_learning_chronology_rejects_nonzero_submicrosecond_timestamp_precision() -> None:
    with unittest.TestCase().assertRaisesRegex(
        LearningEnvironmentError, "precision finer than microseconds"
    ):
        CausalLearningEnvironmentTests._identity(
            cutoff_ts="2026-09-17T14:00:00.1234561+00:00"
        )

    identity = CausalLearningEnvironmentTests._identity()
    with unittest.TestCase().assertRaisesRegex(
        LearningEnvironmentError, "precision finer than microseconds"
    ):
        CausalLearningEnvironmentTests._observation(
            identity.environment_id,
            observed_at="2026-09-17T13:00:00.1234561+00:00",
        )
