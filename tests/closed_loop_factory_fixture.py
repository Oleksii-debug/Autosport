import hashlib
import json

from autosport.scientific_registry import (
    DatasetSnapshot,
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
    FactoryCandidateSpec,
    PromotionRule,
    TrainingPoint,
    WalkForwardEvaluationConfig,
    training_points_manifest_sha256,
)

SOURCE_SHA = "a" * 64
CONFIG_SHA = "b" * 64
FACTORY_SHA = "c" * 64

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"
T5 = "2026-01-06T00:00:00+00:00"
T6 = "2026-01-07T00:00:00+00:00"
T7 = "2026-01-08T00:00:00+00:00"
T8 = "2026-01-09T00:00:00+00:00"


def _canonical_sha(payload):
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _payload_sha(record):
    return _canonical_sha(record.to_payload())


def candidate_points():
    return (
        TrainingPoint(T0, 1.0, 0.0, T0),
        TrainingPoint(T1, 2.0, 1.0, T1),
        TrainingPoint(T4, 3.0, 1.0, T4),
        TrainingPoint(T5, 4.0, 0.0, T5),
    )


def _promotion_evidence(
    *,
    question_id,
    hypothesis_id,
    experiment_id,
    strategy_id,
    model_id,
    bundle_id,
    dataset_id,
    protocol_id,
    bundle_sha,
    rollback_identity,
    dataset_manifest_sha256,
):
    payload = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "research_protocol_id": protocol_id,
        "research_question_id": question_id,
        "hypothesis_id": hypothesis_id,
        "candidate_strategy_version_id": strategy_id,
        "candidate_model_version_id": model_id,
        "evaluation_bundle_id": bundle_id,
        "evaluation_bundle_sha256": bundle_sha,
        "dataset_snapshot_id": dataset_id,
        "holdout_access_id": promotion_holdout_access_id(
            research_protocol_id=protocol_id,
            dataset_manifest_sha256=dataset_manifest_sha256,
            source_identity="lawful-provider:fixture",
            license_identity="license-evidence:v1",
            confirmation_trial_family_id=f"{protocol_id}:confirmation-trial-family",
        ),
        "confirmation_trial_family_id": f"{protocol_id}:confirmation-trial-family",
        "estimand": "mse",
        "direction": PromotionEvidenceDirection.LOWER_IS_BETTER.value,
        "cohort_id": dataset_id,
        "effective_sample_size": 5,
        "minimum_effective_sample_size": 2,
        "effect_interval_low": "0.1",
        "effect_interval_high": "0.2",
        "practical_improvement": "0.15",
        "guardrails_passed": True,
        "validity": PromotionEvidenceValidity.ELIGIBLE.value,
        "holdout_consumed": False,
        "stopping_rule_sha256": hashlib.sha256(b"one final evaluation").hexdigest(),
        "multiple_comparison_control_sha256": hashlib.sha256(
            b"single frozen primary metric"
        ).hexdigest(),
        "rollback_identity": rollback_identity,
        "uncertainty_method": "paired min/max interval",
        "created_at": T3,
    }
    evidence_id = _canonical_sha(payload)
    fields = {key: value for key, value in payload.items() if key != "schema_version"}
    fields["direction"] = PromotionEvidenceDirection(payload["direction"])
    fields["validity"] = PromotionEvidenceValidity(payload["validity"])
    return PromotionEvidence(evidence_id, **fields)


def build_closed_loop_factory(tmp_path, registry, question, environment_id):
    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be ScientificRegistry")
    if not isinstance(question, ResearchQuestion):
        raise TypeError("question must be ResearchQuestion")

    points = candidate_points()
    evaluator_config = WalkForwardEvaluationConfig(
        2,
        feature_set_id="features-closed-loop",
        feature_definition_sha256=CONFIG_SHA,
        feature_source_sha256=FACTORY_SHA,
    )
    manifest = training_points_manifest_sha256(points)
    rule = PromotionRule("mse", 0.05, (("max_squared_error", 0.50),))
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")

    hypothesis = Hypothesis(
        "hypothesis-closed-loop",
        question.question_id,
        "The bounded challenger lowers MSE without protective regression.",
        "mse improves by the frozen threshold",
        "mse misses threshold or max_squared_error exceeds guardrail",
        "mse",
        ("max_squared_error",),
        T0,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="protocol-closed-loop",
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
        robustness_checks=("protective metric", "time order"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=rule.frozen_text,
        expected_artifacts=("evaluation bundle", "model artifact", "promotion decision"),
        code_config_sha256=CONFIG_SHA,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, FACTORY_SHA, environment_id, manifest, T0)
    dataset = DatasetSnapshot(
        "dataset-closed-loop",
        manifest,
        "lawful-provider:fixture",
        "license-evidence:v1",
        T2,
        T0,
        outcome_reveal_after=T2,
    )
    features = FeatureSet(
        "features-closed-loop", "v1", CONFIG_SHA, FACTORY_SHA, T0
    )
    for record in (hypothesis, protocol, dataset, features):
        registry.append(record)

    champion_model = ModelVersion(
        "model-closed-loop-v1",
        "fixture-champion",
        SOURCE_SHA,
        FACTORY_SHA,
        environment_id,
        dataset.dataset_snapshot_id,
        features.feature_set_id,
        binding.research_protocol_id,
        7,
        CONFIG_SHA,
        T2,
    )
    champion_strategy = StrategyVersion(
        "strategy-closed-loop-v1",
        "canonical-closed-loop-strategy",
        FACTORY_SHA,
        environment_id,
        CONFIG_SHA,
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
    walk_sha = _canonical_sha(champion_walk_forward)
    metrics_payload = {
        "schema_version": 1,
        "kind": "autosport-factory-metrics-v1",
        "evaluation_bundle_id": "eval-closed-loop-v1",
        "strategy_version_id": champion_strategy.strategy_version_id,
        "model_version_id": champion_model.model_version_id,
        "metrics": {"max_squared_error": 1.0, "mse": 0.80},
        "source": "causal-walk-forward-v1",
        "walk_forward_result_sha256": walk_sha,
        "evaluator_config_sha256": evaluator_config.config_sha256,
        "training_points_manifest_sha256": manifest,
    }
    metrics_sha = store.write("metrics", "eval-closed-loop-v1", metrics_payload)
    evaluation_payload = {
        "schema_version": 1,
        "kind": "autosport-strategy-model-factory-evaluation",
        "evaluation_bundle_id": "eval-closed-loop-v1",
        "experiment_id": "experiment-closed-loop-v1",
        "research_protocol_id": binding.research_protocol_id,
        "protocol_sha256": protocol.protocol_sha256,
        "dataset_snapshot_id": dataset.dataset_snapshot_id,
        "feature_set_id": features.feature_set_id,
        "model_version_id": champion_model.model_version_id,
        "strategy_version_id": champion_strategy.strategy_version_id,
        "evaluator_source_sha256": FACTORY_SHA,
        "evaluator_config": evaluator_config.canonical_payload(),
        "evaluator_config_sha256": evaluator_config.config_sha256,
        "training_points_manifest_sha256": manifest,
        "walk_forward": champion_walk_forward,
        "walk_forward_result_sha256": walk_sha,
        "candidate_metrics": {"max_squared_error": 1.0, "mse": 0.80},
        "candidate_metrics_artifact_sha256": metrics_sha,
        "candidate_metrics_source": "causal-walk-forward-v1",
    }
    evaluation_sha = store.write(
        "evaluation", "eval-closed-loop-v1", evaluation_payload
    )
    champion_bundle = EvaluationBundleRef(
        "eval-closed-loop-v1",
        evaluation_sha,
        FACTORY_SHA,
        dataset.dataset_snapshot_id,
        protocol.protocol_sha256,
        (SOURCE_SHA, metrics_sha),
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
        "experiment-closed-loop-v1",
        binding.research_protocol_id,
        dataset.dataset_snapshot_id,
        features.feature_set_id,
        champion_strategy.strategy_version_id,
        champion_bundle.evaluation_bundle_id,
        7,
        CONFIG_SHA,
        ResearchOutcome.POSITIVE,
        T2,
        model_version_id=champion_model.model_version_id,
        completed_at=T3,
        notes="closed-loop baseline champion",
    )
    registry.append(champion_experiment)
    evidence = _promotion_evidence(
        question_id=question.question_id,
        hypothesis_id=hypothesis.hypothesis_id,
        experiment_id=champion_experiment.experiment_id,
        strategy_id=champion_strategy.strategy_version_id,
        model_id=champion_model.model_version_id,
        bundle_id=champion_bundle.evaluation_bundle_id,
        dataset_id=dataset.dataset_snapshot_id,
        protocol_id=binding.research_protocol_id,
        bundle_sha=evaluation_sha,
        rollback_identity="NONE",
        dataset_manifest_sha256=manifest,
    )
    registry.append(evidence)
    registry.record_promotion(
        PromotionDecision(
            "promotion-closed-loop-v1",
            PromotionAction.PROMOTE,
            champion_strategy.strategy_version_id,
            binding.research_protocol_id,
            protocol.protocol_sha256,
            champion_bundle.evaluation_bundle_id,
            evaluation_sha,
            T3,
            candidate_model_version_id=champion_model.model_version_id,
            promotion_evidence_id=evidence.promotion_evidence_id,
            reason="closed-loop fixture champion",
        )
    )
    spec = FactoryCandidateSpec(
        "experiment-closed-loop-v2",
        "model-closed-loop-v2",
        "strategy-closed-loop-v2",
        "eval-closed-loop-v2",
        "promotion-closed-loop-v2",
        "canonical-closed-loop-strategy",
        binding.research_protocol_id,
        dataset.dataset_snapshot_id,
        features.feature_set_id,
        FACTORY_SHA,
        environment_id,
        FACTORY_SHA,
        11,
        T4,
        T6,
        T7,
        champion_strategy.strategy_version_id,
        champion_model.model_version_id,
    )
    return ExperimentRunner(registry, store), rule, spec, points
