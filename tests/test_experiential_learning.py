import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.experiential_learning import PolicyRetestSpec, run_policy_retest
from autosport.learning_environment import Action, EvidenceTruth, RewardEvidence, Transition
from autosport.policy_update_authority import UtilityBoundUpdateEvidence
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import (
    ExperimentRunner,
    FactoryArtifactStore,
    FactoryRunResult,
    PromotionAction,
    PromotionRule,
    PromotionVerdict,
)
from autosport.transparent_bandit_policy import BanditPolicyState


class ExperientialLearningFactoryBridgeTests(unittest.TestCase):
    def _lineage(self):
        predecessor = BanditPolicyState.initial(
            environment_id="d" * 64,
            protocol_id="protocol-experiential-v1",
            config_sha256="c" * 64,
            seed=17,
            action_types=frozenset({"WAIT"}),
        )
        action = Action(
            environment_id=predecessor.environment_id,
            observation_id="1" * 64,
            action_type="WAIT",
            decided_at="2026-09-17T13:00:00+00:00",
        )
        reward = RewardEvidence(
            environment_id=predecessor.environment_id,
            action_id=action.action_id,
            outcome_id="2" * 64,
            reward=Decimal("0"),
            available_at="2026-09-17T13:00:01+00:00",
            truth=EvidenceTruth.OBSERVED,
        )
        transition = Transition(
            environment_id=predecessor.environment_id,
            episode_id="3" * 64,
            step_index=1,
            observation_id=action.observation_id,
            action_id=action.action_id,
            outcome_id=reward.outcome_id,
            reward_id=reward.reward_id,
            decision_at=action.decided_at,
            resolved_at="2026-09-17T13:00:02+00:00",
        )
        successor, evidence = predecessor.update(
            action=action,
            reward=reward,
            transition=transition,
        )
        return predecessor, successor, evidence

    @staticmethod
    def _protocol_entry(policy: BanditPolicyState, *, config_sha256: str | None = None):
        return SimpleNamespace(
            payload={
                "binding": {
                    "code_config_sha256": config_sha256 or policy.config_sha256,
                },
                "environment_sha256": policy.environment_id,
            }
        )

    def _spec(self):
        return PolicyRetestSpec(
            experiment_id="experiment-experiential-v1",
            model_version_id="model-experiential-v1",
            evaluation_bundle_id="evaluation-experiential-v1",
            promotion_decision_id="promotion-experiential-v1",
            canonical_strategy_id="canonical-experiential-policy",
            dataset_snapshot_id="dataset-experiential-v1",
            feature_set_id="features-experiential-v1",
            source_sha256="f" * 64,
            evaluator_source_sha256="9" * 64,
            created_at="2026-09-17T13:10:00+00:00",
            completed_at="2026-09-17T13:20:00+00:00",
            decided_at="2026-09-17T13:21:00+00:00",
            predecessor_strategy_version_id="strategy-champion-v1",
            predecessor_model_version_id="model-champion-v1",
        )

    def test_factory_spec_binds_exact_policy_environment_protocol_seed_and_identity(self) -> None:
        _, successor, _ = self._lineage()
        factory_spec = self._spec().factory_spec(successor)

        self.assertEqual(factory_spec.strategy_version_id, successor.policy_id)
        self.assertEqual(factory_spec.environment_sha256, successor.environment_id)
        self.assertEqual(factory_spec.research_protocol_id, successor.protocol_id)
        self.assertEqual(factory_spec.seed, successor.seed)

    def test_retest_without_policy_specific_cases_fails_before_factory_mutation(self) -> None:
        predecessor, successor, evidence = self._lineage()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(root / "scientific_registry.json")
            runner = ExperimentRunner(registry, FactoryArtifactStore(root / "artifacts"))
            with (
                patch.object(
                    ScientificRegistry,
                    "get",
                    autospec=True,
                    return_value=self._protocol_entry(successor),
                ),
                patch.object(
                    ExperimentRunner,
                    "run_policy_candidate",
                    autospec=True,
                ) as delegated,
            ):
                with self.assertRaisesRegex(
                    ValueError, "policy-specific causal evaluation cases are required"
                ):
                    run_policy_retest(
                        runner,
                        predecessor_policy=predecessor,
                        challenger_policy=successor,
                        update_evidence=evidence,
                        spec=self._spec(),
                        points=(),
                        rule=PromotionRule("mse", 0.0),
                    )
                self.assertFalse(
                    runner.artifact_store.exists(
                        "transparent-bandit-policy",
                        successor.policy_id,
                    )
                )
                delegated.assert_not_called()

    def test_retest_rejects_policy_config_not_frozen_in_protocol(self) -> None:
        predecessor, successor, evidence = self._lineage()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(root / "scientific_registry.json")
            runner = ExperimentRunner(registry, FactoryArtifactStore(root / "artifacts"))
            with patch.object(
                ScientificRegistry,
                "get",
                autospec=True,
                return_value=self._protocol_entry(successor, config_sha256="4" * 64),
            ):
                with self.assertRaisesRegex(ValueError, "config does not match frozen"):
                    run_policy_retest(
                        runner,
                        predecessor_policy=predecessor,
                        challenger_policy=successor,
                        update_evidence=evidence,
                        spec=self._spec(),
                        points=(),
                        rule=PromotionRule("mse", 0.0),
                    )

    def test_retest_rejects_raw_reward_challenger_without_utility_authority(self) -> None:
        predecessor, successor, evidence = self._lineage()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(root / "scientific_registry.json")
            runner = ExperimentRunner(registry, FactoryArtifactStore(root / "artifacts"))
            with (
                patch.object(
                    ScientificRegistry,
                    "get",
                    autospec=True,
                    return_value=self._protocol_entry(successor),
                ),
                patch.object(
                    ExperimentRunner,
                    "run_policy_candidate",
                    autospec=True,
                ) as delegated,
            ):
                with self.assertRaisesRegex(
                    ValueError, "requires utility-bound update provenance"
                ):
                    run_policy_retest(
                        runner,
                        predecessor_policy=predecessor,
                        challenger_policy=successor,
                        update_evidence=evidence,
                        spec=self._spec(),
                        points=(),
                        rule=PromotionRule("mse", 0.0),
                        evaluation_cases=(),
                    )
                self.assertFalse(
                    runner.artifact_store.exists(
                        "transparent-bandit-policy",
                        successor.policy_id,
                    )
                )
                delegated.assert_not_called()

    def test_retest_rejects_utility_evidence_subclass_before_field_admission(self) -> None:
        predecessor, successor, evidence = self._lineage()
        transition = evidence.transition
        self.assertIsNotNone(transition)

        class ForgedUtilityBoundUpdateEvidence(UtilityBoundUpdateEvidence):
            def __init__(self) -> None:
                # Deliberately bypass the base dataclass initializer/post-init exactly as
                # an in-process spoof would: the forged subtype claims a positive successor
                # and removes the schema-v1 blocking reasons.
                object.__setattr__(self, "environment_id", predecessor.environment_id)
                object.__setattr__(self, "episode_id", transition.episode_id)
                object.__setattr__(self, "transition_id", evidence.transition_id)
                object.__setattr__(self, "action_id", evidence.action_id)
                object.__setattr__(self, "reward_id", evidence.reward_id)
                object.__setattr__(self, "utility_evidence_id", "4" * 64)
                object.__setattr__(self, "utility_semantic_key", "test.forged-utility-v1")
                object.__setattr__(self, "predecessor_policy_id", predecessor.policy_id)
                object.__setattr__(self, "successor_policy_id", successor.policy_id)
                object.__setattr__(self, "reason_codes", ())

        forged = ForgedUtilityBoundUpdateEvidence()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(root / "scientific_registry.json")
            runner = ExperimentRunner(registry, FactoryArtifactStore(root / "artifacts"))
            with patch.object(
                ScientificRegistry,
                "get",
                autospec=True,
                return_value=self._protocol_entry(successor),
            ):
                with self.assertRaisesRegex(
                    TypeError, "must be exact UtilityBoundUpdateEvidence"
                ):
                    run_policy_retest(
                        runner,
                        predecessor_policy=predecessor,
                        challenger_policy=successor,
                        update_evidence=evidence,
                        spec=self._spec(),
                        points=(),
                        rule=PromotionRule("mse", 0.0),
                        utility_update_evidence=forged,
                        evaluation_cases=(),
                    )

    def test_retest_rejects_policy_successor_rebinding_before_factory_call(self) -> None:
        predecessor, successor, evidence = self._lineage()
        rebound = BanditPolicyState.initial(
            environment_id=successor.environment_id,
            protocol_id=successor.protocol_id,
            config_sha256=successor.config_sha256,
            seed=18,
            action_types=frozenset({"WAIT"}),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(root / "scientific_registry.json")
            runner = ExperimentRunner(registry, FactoryArtifactStore(root / "artifacts"))
            with self.assertRaisesRegex(ValueError, "predecessor identity mismatch"):
                run_policy_retest(
                    runner,
                    predecessor_policy=predecessor,
                    challenger_policy=rebound,
                    update_evidence=evidence,
                    spec=self._spec(),
                    points=(),
                    rule=PromotionRule("mse", 0.0),
                )


if __name__ == "__main__":
    unittest.main()
