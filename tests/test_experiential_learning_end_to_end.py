import hashlib
import json
from decimal import Decimal

import pytest

from autosport.experiential_learning import PolicyRetestSpec, run_policy_retest
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.scientific_registry import (
    DatasetSnapshot,
    DuplicateExperimentFingerprintError,
    EvaluationBundleRef,
    ExperimentRecord,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    PromotionAction,
    PromotionDecision,
    PromotionEvidence,
    PromotionEvidenceDirection,
    PromotionEvidenceValidity,
    promotion_holdout_access_id,
    ResearchOutcome,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
    StrategyVersion,
)
from autosport.strategy_experiment import ScientificProtocolBinding
from autosport.strategy_model_factory import (
    ExperimentRunner,
    FactoryArtifactStore,
    PromotionRule,
    PromotionVerdict,
    TrainingPoint,
    WalkForwardEvaluationConfig,
    training_points_manifest_sha256,
)
from autosport.transparent_bandit_policy import BanditPolicyState


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"
T5 = "2026-01-06T00:00:00+00:00"
T6 = "2026-01-07T00:00:00+00:00"
T7 = "2026-01-08T00:00:00+00:00"


def _canonical_sha(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _payload_sha(record: object) -> str:
    return _canonical_sha(record.to_payload())


def _candidate_points() -> tuple[TrainingPoint, ...]:
    # Deliberately poor challenger evidence: the frozen causal evaluator will reject it.
    return (
        TrainingPoint(T0, 1.0, 0.0, T0),
        TrainingPoint(T1, 2.0, 1.0, T1),
        TrainingPoint(T4, 3.0, 10.0, T4),
        TrainingPoint(T5, 4.0, 10.0, T5),
    )


def _resolved_policy_successor() -> tuple[
    EnvironmentIdentity,
    BanditPolicyState,
    BanditPolicyState,
    object,
]:
    identity = EnvironmentIdentity(
        source_id="lawful-provider:fixture",
        config_id="learning-config-v1",
        data_id="dataset-factory",
        protocol_id="protocol-factory",
        cutoff_ts=T2,
        seed=11,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="episode-learned-reject",
        policy_id="transparent-bandit-bootstrap",
        admissible_actions=frozenset({"WAIT"}),
    )
    predecessor = BanditPolicyState.initial(
        environment_id=identity.environment_id,
        protocol_id="protocol-factory",
        config_sha256=SHA_B,
        seed=11,
        action_types=frozenset({"WAIT"}),
    )
    observation = Observation(
        environment_id=identity.environment_id,
        observed_at=T1,
        available_at=T1,
        evidence=(("market_state", "frozen-paper-snapshot"),),
    )
    action = environment.act(
        observation,
        action_type=predecessor.choose(admissible_actions=frozenset({"WAIT"})),
        decision_at="2026-01-02T00:00:01+00:00",
    )
    outcome = Outcome(
        environment_id=identity.environment_id,
        action_id=action.action_id,
        revealed_at=T2,
        truth=EvidenceTruth.OBSERVED,
        evidence=(("match_result", "observed-fixture"),),
    )
    reward = RewardEvidence(
        environment_id=identity.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("-1"),
        available_at="2026-01-03T00:00:01+00:00",
        truth=EvidenceTruth.OBSERVED,
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at="2026-01-03T00:00:02+00:00",
    )
    challenger, update = predecessor.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    return identity, predecessor, challenger, update


def _real_factory_foundation(tmp_path, identity: EnvironmentIdentity):
    points = _candidate_points()
    manifest_sha256 = training_points_manifest_sha256(points)
    evaluator_config = WalkForwardEvaluationConfig(
        2,
        feature_set_id="features-factory",
        feature_definition_sha256=SHA_B,
        feature_source_sha256=SHA_C,
    )
    rule = PromotionRule("mse", 0.05, (("max_squared_error", 0.50),))
    registry_path = tmp_path / "scientific_registry.json"
    artifact_root = tmp_path / "factory-artifacts"
    registry = ScientificRegistry.initialize_pristine(registry_path)
    store = FactoryArtifactStore(artifact_root)

    question = ResearchQuestion(
        "question-factory",
        "Does the learned challenger lower frozen holdout MSE without guardrail regression?",
        SHA_A,
        T0,
    )
    hypothesis = Hypothesis(
        "hypothesis-factory",
        "question-factory",
        "Learned challenger lowers MSE while maximum fold squared error remains bounded.",
        "mse improves by the frozen threshold",
        "mse misses threshold or max_squared_error exceeds guardrail",
        "mse",
        ("max_squared_error",),
        T0,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="protocol-factory",
        research_question_id=question.question_id,
        research_question_sha256=_payload_sha(question),
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=_payload_sha(hypothesis),
        inclusion_criteria="predeclared governed examples",
        exclusion_criteria="missing provenance or causal availability",
        lawful_source_requirements="lawful retained source evidence",
        causal_cutoff=T2,
        evaluation_design=evaluator_config.frozen_text,
        feature_set_version="v1",
        uncertainty_method="paired min/max interval",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time order", "protective metric"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=rule.frozen_text,
        expected_artifacts=("model artifact", "evaluation bundle", "promotion decision"),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(
        binding,
        SHA_C,
        identity.environment_id,
        manifest_sha256,
        T0,
    )
    dataset = DatasetSnapshot(
        "dataset-factory",
        manifest_sha256,
        "lawful-provider:fixture",
        "license-evidence:v1",
        T2,
        T0,
        outcome_reveal_after=T2,
    )
    features = FeatureSet("features-factory", "v1", SHA_B, SHA_C, T0)
    for record in (question, hypothesis, protocol, dataset, features):
        registry.append(record)

    champion_model = ModelVersion(
        "model-v1",
        "fixture-champion",
        SHA_A,
        SHA_C,
        identity.environment_id,
        dataset.dataset_snapshot_id,
        features.feature_set_id,
        binding.research_protocol_id,
        7,
        SHA_B,
        T2,
    )
    champion_strategy = StrategyVersion(
        "strategy-v1",
        "canonical-factory-strategy",
        SHA_C,
        identity.environment_id,
        SHA_B,
        T2,
        model_version_id=champion_model.model_version_id,
    )
    champion_walk_forward = {
        "model_family": "mean-baseline-v1",
        "primary_metric": "mse",
        "primary_value": 0.80,
        "folds": [
            {
                "fold_id": "champion-fold-1",
                "training_cutoff": T1,
                "evaluation_at": T2,
                "target_available_at": T2,
                "causal_training_count": 2,
                "prediction": 0.0,
                "target": 1.0,
                "squared_error": 1.0,
            },
            {
                "fold_id": "champion-fold-2",
                "training_cutoff": T2,
                "evaluation_at": T3,
                "target_available_at": T3,
                "causal_training_count": 3,
                "prediction": 0.0,
                "target": 1.0,
                "squared_error": 1.0,
            },
            {
                "fold_id": "champion-fold-3",
                "training_cutoff": T3,
                "evaluation_at": T4,
                "target_available_at": T4,
                "causal_training_count": 4,
                "prediction": 0.0,
                "target": 1.0,
                "squared_error": 1.0,
            },
            {
                "fold_id": "champion-fold-4",
                "training_cutoff": T4,
                "evaluation_at": T5,
                "target_available_at": T5,
                "causal_training_count": 5,
                "prediction": 0.0,
                "target": 0.0,
                "squared_error": 0.0,
            },
        ],
    }
    champion_walk_forward_sha256 = _canonical_sha(champion_walk_forward)
    champion_metrics_payload = {
        "schema_version": 1,
        "kind": "autosport-factory-metrics-v1",
        "evaluation_bundle_id": "eval-v1",
        "strategy_version_id": champion_strategy.strategy_version_id,
        "model_version_id": champion_model.model_version_id,
        "metrics": {"max_squared_error": 1.0, "mse": 0.80},
        "source": "causal-walk-forward-v1",
        "walk_forward_result_sha256": champion_walk_forward_sha256,
        "evaluator_config_sha256": evaluator_config.config_sha256,
        "training_points_manifest_sha256": manifest_sha256,
    }
    champion_metrics_sha256 = store.write("metrics", "eval-v1", champion_metrics_payload)
    champion_evaluation_payload = {
        "schema_version": 1,
        "kind": "autosport-strategy-model-factory-evaluation",
        "evaluation_bundle_id": "eval-v1",
        "experiment_id": "experiment-v1",
        "research_protocol_id": binding.research_protocol_id,
        "protocol_sha256": protocol.protocol_sha256,
        "dataset_snapshot_id": dataset.dataset_snapshot_id,
        "feature_set_id": features.feature_set_id,
        "model_version_id": champion_model.model_version_id,
        "strategy_version_id": champion_strategy.strategy_version_id,
        "evaluator_source_sha256": SHA_C,
        "evaluator_config": evaluator_config.canonical_payload(),
        "evaluator_config_sha256": evaluator_config.config_sha256,
        "training_points_manifest_sha256": manifest_sha256,
        "walk_forward": champion_walk_forward,
        "walk_forward_result_sha256": champion_walk_forward_sha256,
        "candidate_metrics": {"max_squared_error": 1.0, "mse": 0.80},
        "candidate_metrics_artifact_sha256": champion_metrics_sha256,
        "candidate_metrics_source": "causal-walk-forward-v1",
    }
    champion_evaluation_sha256 = store.write(
        "evaluation", "eval-v1", champion_evaluation_payload
    )
    champion_bundle = EvaluationBundleRef(
        "eval-v1",
        champion_evaluation_sha256,
        SHA_C,
        dataset.dataset_snapshot_id,
        protocol.protocol_sha256,
        (SHA_A, champion_metrics_sha256),
        T3,
        evaluated_strategy_version_id=champion_strategy.strategy_version_id,
        evaluated_model_version_id=champion_model.model_version_id,
        effective_sample_size=5,
        effect_interval_low="0.1",
        effect_interval_high="0.2",
        practical_improvement="0.15",
    )
    for record in (champion_model, champion_strategy, champion_bundle):
        registry.append(record)
    champion_experiment = ExperimentRecord(
        "experiment-v1",
        binding.research_protocol_id,
        dataset.dataset_snapshot_id,
        features.feature_set_id,
        champion_strategy.strategy_version_id,
        champion_bundle.evaluation_bundle_id,
        7,
        SHA_B,
        ResearchOutcome.POSITIVE,
        T2,
        model_version_id=champion_model.model_version_id,
        completed_at=T3,
        notes="fixture champion",
    )
    registry.append(champion_experiment)
    confirmation_trial_family_id = (
        f"{binding.research_protocol_id}:confirmation-trial-family"
    )
    champion_evidence_payload = {
        "schema_version": 1,
        "experiment_id": champion_experiment.experiment_id,
        "research_protocol_id": binding.research_protocol_id,
        "research_question_id": question.question_id,
        "hypothesis_id": hypothesis.hypothesis_id,
        "candidate_strategy_version_id": champion_strategy.strategy_version_id,
        "candidate_model_version_id": champion_model.model_version_id,
        "evaluation_bundle_id": champion_bundle.evaluation_bundle_id,
        "evaluation_bundle_sha256": champion_evaluation_sha256,
        "dataset_snapshot_id": dataset.dataset_snapshot_id,
        "holdout_access_id": promotion_holdout_access_id(
            research_protocol_id=binding.research_protocol_id,
            dataset_manifest_sha256=manifest_sha256,
            source_identity="lawful-provider:fixture",
            license_identity="license-evidence:v1",
            confirmation_trial_family_id=confirmation_trial_family_id,
        ),
        "confirmation_trial_family_id": confirmation_trial_family_id,
        "estimand": "mse",
        "direction": PromotionEvidenceDirection.LOWER_IS_BETTER.value,
        "cohort_id": dataset.dataset_snapshot_id,
        "effective_sample_size": 5,
        "minimum_effective_sample_size": 2,
        "effect_interval_low": "0.1",
        "effect_interval_high": "0.2",
        "practical_improvement": "0.15",
        "guardrails_passed": True,
        "validity": PromotionEvidenceValidity.ELIGIBLE.value,
        "holdout_consumed": False,
        "stopping_rule_sha256": hashlib.sha256(
            b"one final evaluation"
        ).hexdigest(),
        "multiple_comparison_control_sha256": hashlib.sha256(
            b"single frozen primary metric"
        ).hexdigest(),
        "rollback_identity": "NONE",
        "uncertainty_method": "paired min/max interval",
        "created_at": T3,
    }
    champion_evidence_id = _canonical_sha(champion_evidence_payload)
    champion_evidence_fields = {
        key: value
        for key, value in champion_evidence_payload.items()
        if key != "schema_version"
    }
    champion_evidence_fields["direction"] = PromotionEvidenceDirection(
        champion_evidence_fields["direction"]
    )
    champion_evidence_fields["validity"] = PromotionEvidenceValidity(
        champion_evidence_fields["validity"]
    )
    champion_evidence = PromotionEvidence(
        champion_evidence_id,
        **champion_evidence_fields,
    )
    registry.append(champion_evidence)
    registry.record_promotion(
        PromotionDecision(
            "promotion-v1",
            PromotionAction.PROMOTE,
            champion_strategy.strategy_version_id,
            binding.research_protocol_id,
            protocol.protocol_sha256,
            champion_bundle.evaluation_bundle_id,
            champion_evaluation_sha256,
            T3,
            candidate_model_version_id=champion_model.model_version_id,
            promotion_evidence_id=champion_evidence.promotion_evidence_id,
            reason="fixture baseline champion",
        )
    )
    return registry, registry_path, artifact_root, store, rule, points


def _retest_spec(*, experiment_id: str, model_version_id: str, evaluation_id: str, promotion_id: str):
    return PolicyRetestSpec(
        experiment_id=experiment_id,
        model_version_id=model_version_id,
        evaluation_bundle_id=evaluation_id,
        promotion_decision_id=promotion_id,
        canonical_strategy_id="canonical-factory-strategy",
        dataset_snapshot_id="dataset-factory",
        feature_set_id="features-factory",
        source_sha256=SHA_C,
        evaluator_source_sha256=SHA_C,
        created_at=T4,
        completed_at=T6,
        decided_at=T7,
        predecessor_strategy_version_id="strategy-v1",
        predecessor_model_version_id="model-v1",
    )


def test_legacy_baseline_policy_retest_fails_closed_without_registry_mutation(tmp_path):
    identity, predecessor, challenger, update = _resolved_policy_successor()
    registry, registry_path, artifact_root, store, rule, points = _real_factory_foundation(
        tmp_path, identity
    )
    runner = ExperimentRunner(registry, store)
    spec = _retest_spec(
        experiment_id="experiment-learned-reject",
        model_version_id="model-learned-reject",
        evaluation_id="eval-learned-reject",
        promotion_id="promotion-learned-reject",
    )

    with pytest.raises(
        ValueError, match="baseline TrainingPoint evidence cannot authorize"
    ):
        run_policy_retest(
            runner,
            predecessor_policy=predecessor,
            challenger_policy=challenger,
            update_evidence=update,
            spec=spec,
            points=points,
            rule=rule,
        )

    reopened = ScientificRegistry(registry_path)
    assert reopened.get("Experiment", spec.experiment_id) is None
    assert reopened.get("PromotionDecision", spec.promotion_decision_id) is None
    assert not store.exists("transparent-bandit-policy", challenger.policy_id)
