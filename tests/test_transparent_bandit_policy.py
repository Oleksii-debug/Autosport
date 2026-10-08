import hashlib
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch
from fractions import Fraction

import autosport.transparent_bandit_policy as policy_module
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    LearningEnvironmentError,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.transparent_bandit_policy import ActionEstimate, BanditPolicyState


class TransparentBanditPolicyTests(unittest.TestCase):
    def test_action_estimate_rejects_rebound_decimal_root_before_dispatch(self) -> None:
        hostile_calls: list[str] = []

        class ForgedDecimal:
            def is_finite(self):
                hostile_calls.append("is_finite")
                raise AssertionError("forged Decimal dispatch executed")

        forged = object.__new__(ForgedDecimal)
        with patch.object(policy_module, "Decimal", ForgedDecimal):
            with self.assertRaisesRegex(
                LearningEnvironmentError,
                "finite exact Decimal",
            ):
                ActionEstimate("PAPER_PROPOSAL", 1, forged)

        self.assertEqual(hostile_calls, [])

    def test_policy_state_rejects_rebound_estimate_root_before_dispatch(self) -> None:
        policy, _action, _reward, _transition = self._resolved_paper_step()
        hostile_calls: list[str] = []

        class ForgedEstimate:
            def __getattribute__(self, name):
                hostile_calls.append(name)
                raise AssertionError("forged ActionEstimate dispatch executed")

        forged = object.__new__(ForgedEstimate)
        with patch.object(policy_module, "ActionEstimate", ForgedEstimate):
            with self.assertRaisesRegex(
                LearningEnvironmentError,
                "exact ActionEstimate",
            ):
                replace(policy, estimates=(forged,))

        self.assertEqual(hostile_calls, [])

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


    def test_policy_rejects_estimate_subclass_that_can_change_selection_behavior(self) -> None:
        class HostileEstimate(ActionEstimate):
            @property
            def selection_score(self) -> Fraction:
                return Fraction(999)

        canonical = ActionEstimate("WAIT", 0, Decimal("0"))
        hostile = HostileEstimate("WAIT", 0, Decimal("0"))
        self.assertEqual(hostile.to_payload(), canonical.to_payload())

        with self.assertRaisesRegex(
            LearningEnvironmentError,
            "exact ActionEstimate",
        ):
            BanditPolicyState(
                environment_id="a" * 64,
                protocol_id="policy-estimate-type-root-v1",
                config_sha256="b" * 64,
                seed=17,
                generation=0,
                estimates=(hostile,),
            )

    def test_action_estimate_rejects_decimal_subclass_before_virtual_dispatch(self) -> None:
        class HostileDecimal(Decimal):
            def is_finite(self):
                raise AssertionError("Decimal subclass virtual dispatch executed")

        with self.assertRaisesRegex(
            LearningEnvironmentError,
            "finite exact Decimal",
        ):
            ActionEstimate("WAIT", 0, HostileDecimal("0"))

    def test_action_estimate_rejects_integer_subclass_for_observation_identity(self) -> None:
        class HostileInt(int):
            def __add__(self, other):
                raise AssertionError("integer subclass arithmetic executed")

        with self.assertRaisesRegex(
            LearningEnvironmentError,
            "observations must be an integer",
        ):
            ActionEstimate("WAIT", HostileInt(0), Decimal("0"))

    def test_policy_rejects_frozenset_subclass_before_iteration_dispatch(self) -> None:
        class HostileFrozenSet(frozenset):
            def __iter__(self):
                raise AssertionError("frozenset subclass iteration executed")

        with self.assertRaisesRegex(
            LearningEnvironmentError,
            "action_types must be a non-empty frozenset",
        ):
            BanditPolicyState.initial(
                environment_id="a" * 64,
                protocol_id="policy-action-type-root-v1",
                config_sha256="b" * 64,
                seed=17,
                action_types=HostileFrozenSet({"WAIT"}),
            )

        policy = BanditPolicyState.initial(
            environment_id="a" * 64,
            protocol_id="policy-admissibility-root-v1",
            config_sha256="b" * 64,
            seed=17,
            action_types=frozenset({"WAIT"}),
        )
        with self.assertRaisesRegex(
            LearningEnvironmentError,
            "admissible_actions must be a non-empty frozenset",
        ):
            policy.choose(admissible_actions=HostileFrozenSet({"WAIT"}))


if __name__ == "__main__":
    unittest.main()
