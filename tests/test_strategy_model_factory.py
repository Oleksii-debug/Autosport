import hashlib
import json
import math
from dataclasses import replace

import pytest

from autosport._strategy_model_factory_impl import _holdout_consumed_by_other_evidence
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
    DriftEvidence,
    DriftMonitor,
    ExperimentRunner,
    FactoryArtifactStore,
    FactoryCandidateSpec,
    MeanBaselineModel,
    PromotionController,
    PromotionRule,
    PromotionVerdict,
    TrainingPoint,
    WalkForwardEvaluationConfig,
    WalkForwardRunner,
    training_points_manifest_sha256,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"
T5 = "2026-01-06T00:00:00+00:00"
T6 = "2026-01-07T00:00:00+00:00"
T7 = "2026-01-08T00:00:00+00:00"


def _points():
    return (
        TrainingPoint(T0, 1.0, 0.0, T0),
        TrainingPoint(T1, 2.0, 1.0, T1),
        TrainingPoint(T2, 3.0, 1.0, T2),
        TrainingPoint(T3, 4.0, 0.0, T3),
    )


def _candidate_points():
    return (
        TrainingPoint(T0, 1.0, 0.0, T0),
        TrainingPoint(T1, 2.0, 1.0, T1),
        TrainingPoint(T4, 3.0, 1.0, T4),
        TrainingPoint(T5, 4.0, 0.0, T5),
    )


def _bad_candidate_points():
    return (
        TrainingPoint(T0, 1.0, 0.0, T0),
        TrainingPoint(T1, 2.0, 1.0, T1),
        TrainingPoint(T4, 3.0, 10.0, T4),
        TrainingPoint(T5, 4.0, 10.0, T5),
    )


def _guardrail_bad_candidate_points():
    return (
        TrainingPoint(T0, 1.0, 0.0, T0),
        TrainingPoint(T1, 2.0, 0.0, T1),
        TrainingPoint(T4, 3.0, 1.0, T4),
        TrainingPoint(T5, 4.0, 0.5, T5),
    )


def _payload_sha(record) -> str:
    canonical = json.dumps(
        record.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_sha(payload) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _factory_rule() -> PromotionRule:
    return PromotionRule("mse", 0.05, (("max_squared_error", 0.50),))


def _promotion_evidence(
    *,
    experiment_id: str,
    strategy_id: str,
    model_id: str,
    bundle_id: str,
    dataset_id: str,
    protocol_id: str,
    bundle_sha: str,
    rollback_identity: str,
    dataset_manifest_sha256: str,
    created_at: str = T3,
    effective_n: int = 5,
    minimum_n: int = 2,
) -> PromotionEvidence:
    payload = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "research_protocol_id": protocol_id,
        "research_question_id": "question-factory",
        "hypothesis_id": "hypothesis-factory",
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
        "effective_sample_size": effective_n,
        "minimum_effective_sample_size": minimum_n,
        "effect_interval_low": "0.1",
        "effect_interval_high": "0.2",
        "practical_improvement": "0.15",
        "guardrails_passed": True,
        "validity": PromotionEvidenceValidity.ELIGIBLE.value,
        "holdout_consumed": False,
        "stopping_rule_sha256": hashlib.sha256(b"one final evaluation").hexdigest(),
        "multiple_comparison_control_sha256": hashlib.sha256(b"single frozen primary metric").hexdigest(),
        "rollback_identity": rollback_identity,
        "uncertainty_method": "paired min/max interval",
        "created_at": created_at,
    }
    evidence_id = _canonical_sha(payload)
    fields = {key: value for key, value in payload.items() if key != "schema_version"}
    fields["direction"] = PromotionEvidenceDirection(payload["direction"])
    fields["validity"] = PromotionEvidenceValidity(payload["validity"])
    return PromotionEvidence(evidence_id, **fields)


def _factory_foundation(tmp_path, *, points=None, minimum_train_size=2):
    governed_points = _candidate_points() if points is None else tuple(points)
    evaluator_config = WalkForwardEvaluationConfig(
        minimum_train_size,
        feature_set_id="features-factory",
        feature_definition_sha256=SHA_B,
        feature_source_sha256=SHA_C,
    )
    dataset_manifest_sha256 = training_points_manifest_sha256(governed_points)
    rule = _factory_rule()
    registry_path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(registry_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")

    question = ResearchQuestion(
        "question-factory",
        "Does the challenger lower frozen holdout MSE without guardrail regression?",
        SHA_A,
        T0,
    )
    hypothesis = Hypothesis(
        "hypothesis-factory",
        "question-factory",
        "Challenger lowers MSE while maximum fold squared error remains bounded.",
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
        SHA_D,
        dataset_manifest_sha256,
        T0,
    )
    dataset = DatasetSnapshot(
        "dataset-factory",
        dataset_manifest_sha256,
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
        SHA_D,
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
        SHA_D,
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
        "training_points_manifest_sha256": dataset_manifest_sha256,
    }
    champion_metrics_sha256 = store.write(
        "metrics", "eval-v1", champion_metrics_payload
    )
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
        "training_points_manifest_sha256": dataset_manifest_sha256,
        "walk_forward": champion_walk_forward,
        "walk_forward_result_sha256": champion_walk_forward_sha256,
        "candidate_metrics": {"max_squared_error": 1.0, "mse": 0.80},
        "candidate_metrics_artifact_sha256": champion_metrics_sha256,
        "candidate_metrics_source": "causal-walk-forward-v1",
        "promotion_effect_evidence": {
            "schema_version": 1,
            "experiment_id": "experiment-v1",
            "research_protocol_id": binding.research_protocol_id,
            "research_question_id": question.question_id,
            "hypothesis_id": hypothesis.hypothesis_id,
            "candidate_strategy_version_id": champion_strategy.strategy_version_id,
            "candidate_model_version_id": champion_model.model_version_id,
            "evaluation_bundle_id": "eval-v1",
            "evaluation_bundle_sha256": None,
            "dataset_snapshot_id": dataset.dataset_snapshot_id,
            "confirmation_trial_family_id": f"{binding.research_protocol_id}:confirmation-trial-family",
            "holdout_access_id": promotion_holdout_access_id(
                research_protocol_id=binding.research_protocol_id,
                dataset_manifest_sha256=dataset_manifest_sha256,
                source_identity=dataset.source_identity,
                license_identity=dataset.license_identity,
                confirmation_trial_family_id=f"{binding.research_protocol_id}:confirmation-trial-family",
            ),
            "estimand": "mse",
            "direction": PromotionEvidenceDirection.LOWER_IS_BETTER.value,
            "cohort_id": dataset.dataset_snapshot_id,
            "effective_sample_size": 5,
            "minimum_effective_sample_size": rule.minimum_effective_sample_size,
            "effect_interval_low": "0.1",
            "effect_interval_high": "0.2",
            "practical_improvement": "0.15",
            "guardrails_passed": True,
            "validity": PromotionEvidenceValidity.ELIGIBLE.value,
            "holdout_consumed": False,
            "stopping_rule_sha256": hashlib.sha256(b"one final evaluation").hexdigest(),
            "multiple_comparison_control_sha256": hashlib.sha256(b"single frozen primary metric").hexdigest(),
            "rollback_identity": "NONE",
            "uncertainty_method": binding.uncertainty_method,
            "created_at": T3,
        },
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
    champion_evidence = _promotion_evidence(
        experiment_id=champion_experiment.experiment_id,
        strategy_id=champion_strategy.strategy_version_id,
        model_id=champion_model.model_version_id,
        bundle_id=champion_bundle.evaluation_bundle_id,
        dataset_id=dataset.dataset_snapshot_id,
        protocol_id=binding.research_protocol_id,
        bundle_sha=champion_evaluation_sha256,
        rollback_identity="NONE",
        dataset_manifest_sha256=dataset_manifest_sha256,
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
    return registry, registry_path, rule, store, evaluator_config, dataset_manifest_sha256


def _candidate_spec() -> FactoryCandidateSpec:
    return FactoryCandidateSpec(
        "experiment-v2",
        "model-v2",
        "strategy-v2",
        "eval-v2",
        "promotion-v2",
        "canonical-factory-strategy",
        "protocol-factory",
        "dataset-factory",
        "features-factory",
        SHA_C,
        SHA_D,
        SHA_C,
        11,
        T4,
        T6,
        T7,
        "strategy-v1",
        "model-v1",
    )


def _run_candidate(runner, points, rule, **kwargs):
    return runner.run_baseline_candidate(
        _candidate_spec(),
        points,
        rule=rule,
        **kwargs,
    )


class _RecordingMeanFactory:
    model_family = "mean-baseline-v1"

    def __init__(self):
        self.fit_ids = []

    def fit(self, model_id, points, *, training_cutoff):
        self.fit_ids.append(model_id)
        return MeanBaselineModel.fit(
            model_id,
            points,
            training_cutoff=training_cutoff,
        )


def test_mean_baseline_uses_only_observations_at_or_before_training_cutoff():
    model = MeanBaselineModel.fit("baseline-1", _points(), training_cutoff=T1)
    assert model.training_count == 2
    assert model.mean_target == 0.5
    assert len(model.identity_sha256) == 64


def test_mean_baseline_rejects_future_prediction_input():
    model = MeanBaselineModel.fit("baseline-1", _points()[:2], training_cutoff=T1)
    with pytest.raises(ValueError, match="not available"):
        model.predict(TrainingPoint(T3, 4.0, 0.0), decision_at=T2)


def test_baseline_excludes_labels_not_revealed_by_training_cutoff():
    points = (
        TrainingPoint(T0, 1.0, 0.0, T4),
        TrainingPoint(T1, 2.0, 1.0, T1),
    )
    model = MeanBaselineModel.fit("baseline-delayed", points, training_cutoff=T2)
    assert model.training_count == 1
    assert model.mean_target == 1.0


def test_baseline_rejects_missing_target_availability_provenance():
    with pytest.raises(ValueError, match="target_available_at is required"):
        MeanBaselineModel.fit(
            "baseline-unproven-label",
            (TrainingPoint(T0, 1.0, 0.0),),
            training_cutoff=T0,
        )


def test_training_point_rejects_target_reveal_before_observation():
    with pytest.raises(ValueError, match="must not precede observed_at"):
        TrainingPoint(T2, 1.0, 0.0, T1)


def test_training_points_manifest_is_order_invariant_and_content_sensitive():
    points = _candidate_points()
    assert training_points_manifest_sha256(points) == training_points_manifest_sha256(
        tuple(reversed(points))
    )
    altered = points[:-1] + (replace(points[-1], feature=999.0),)
    assert training_points_manifest_sha256(altered) != training_points_manifest_sha256(points)


def test_walk_forward_is_expanding_window_and_deterministic():
    first = WalkForwardRunner.run(_points(), minimum_train_size=2)
    second = WalkForwardRunner.run(tuple(reversed(_points())), minimum_train_size=2)
    assert first == second
    assert first.result_sha256 == second.result_sha256
    assert [fold.training_cutoff for fold in first.folds] == [T1, T2]
    assert [fold.causal_training_count for fold in first.folds] == [2, 3]
    assert all(fold.training_cutoff < fold.evaluation_at for fold in first.folds)
    assert all(fold.target_available_at for fold in first.folds)
    assert first.primary_metric == "mse"


def test_walk_forward_minimum_counts_only_causally_revealed_labels():
    points = (
        TrainingPoint(T0, 1.0, 0.0, T4),
        TrainingPoint(T1, 2.0, 1.0, T1),
        TrainingPoint(T2, 3.0, 1.0, T2),
        TrainingPoint(T3, 4.0, 0.0, T3),
    )
    result = WalkForwardRunner.run(points, minimum_train_size=2)
    assert len(result.folds) == 1
    assert result.folds[0].evaluation_at == T3
    assert result.folds[0].causal_training_count == 2


def test_walk_forward_uses_injected_typed_baseline_factory_boundary():
    factory = _RecordingMeanFactory()
    result = WalkForwardRunner.run(
        _points(), minimum_train_size=2, model_factory=factory
    )
    assert result.model_family == factory.model_family
    assert factory.fit_ids == [
        "mean-baseline-v1-fold-2",
        "mean-baseline-v1-fold-3",
    ]


def test_walk_forward_rejects_duplicate_timestamp_identity():
    points = _points()[:2] + (TrainingPoint(T1, 9.0, 0.0, T1),)
    with pytest.raises(ValueError, match="unique timestamps"):
        WalkForwardRunner.run(points, minimum_train_size=2)


def test_promotion_requires_provenance_rollback_primary_and_protective_metrics():
    rule = PromotionRule("mse", 0.05, (("max_drawdown", 0.20),))
    accepted = PromotionController.evaluate(
        rule,
        champion_metrics={"mse": 0.40, "max_drawdown": 0.10},
        challenger_metrics={"mse": 0.30, "max_drawdown": 0.15},
        provenance_complete=True,
        rollback_target="strategy-v1",
    )
    assert accepted.verdict is PromotionVerdict.INCONCLUSIVE
    assert accepted.registry_action is PromotionAction.RETAIN
    degraded = PromotionController.evaluate(
        rule,
        champion_metrics={"mse": 0.40, "max_drawdown": 0.10},
        challenger_metrics={"mse": 0.20, "max_drawdown": 0.25},
        provenance_complete=True,
        rollback_target="strategy-v1",
    )
    assert degraded.verdict is PromotionVerdict.REJECT
    assert "protective metric degraded: max_drawdown" in degraded.reasons


def test_promotion_controller_rejects_evidence_local_sample_floor_bypass():
    rule = PromotionRule("mse", 0.05, minimum_effective_sample_size=10)
    evidence = _promotion_evidence(
        experiment_id="experiment-controller-floor",
        strategy_id="strategy-controller-floor",
        model_id="model-controller-floor",
        bundle_id="eval-controller-floor",
        dataset_id="dataset-controller-floor",
        protocol_id="protocol-controller-floor",
        bundle_sha=SHA_D,
        rollback_identity="strategy-v1",
        dataset_manifest_sha256=SHA_A,
        effective_n=2,
        minimum_n=1,
    )

    result = PromotionController.evaluate(
        rule,
        champion_metrics={"mse": 0.40},
        challenger_metrics={"mse": 0.20},
        provenance_complete=True,
        rollback_target="strategy-v1",
        promotion_evidence=evidence,
    )

    assert result.verdict is PromotionVerdict.INCONCLUSIVE
    assert result.registry_action is PromotionAction.RETAIN
    assert (
        "promotion evidence minimum sample size does not match frozen rule"
        in result.reasons
    )
    assert "effective sample size below frozen minimum" in result.reasons


def test_holdout_consumption_allows_same_frozen_attempt_retry_only():
    same_attempt = {
        "experiment_id": "experiment-v2",
        "research_protocol_id": "protocol-factory",
        "research_question_id": "question-factory",
        "hypothesis_id": "hypothesis-factory",
        "candidate_strategy_version_id": "strategy-v2",
        "candidate_model_version_id": "model-v2",
        "evaluation_bundle_id": "eval-v2",
        "dataset_snapshot_id": "dataset-factory",
        "confirmation_trial_family_id": "protocol-factory:confirmation-trial-family",
        "holdout_access_id": "holdout-stable",
        "estimand": "mse",
        "direction": PromotionEvidenceDirection.LOWER_IS_BETTER.value,
        "rollback_identity": "strategy-v1",
        "created_at": T7,
    }
    assert not _holdout_consumed_by_other_evidence(
        (dict(same_attempt),),
        same_attempt_identity=same_attempt,
    )

    other_attempt = dict(same_attempt)
    other_attempt["experiment_id"] = "experiment-other"
    assert _holdout_consumed_by_other_evidence(
        (other_attempt,),
        same_attempt_identity=same_attempt,
    )

    unrelated_holdout = dict(other_attempt)
    unrelated_holdout["holdout_access_id"] = "holdout-other"
    assert not _holdout_consumed_by_other_evidence(
        (unrelated_holdout,),
        same_attempt_identity=same_attempt,
    )


def test_factory_rejects_nonfinite_metrics():
    with pytest.raises(ValueError, match="finite"):
        PromotionController.evaluate(
            PromotionRule("mse", 0.0),
            champion_metrics={"mse": 1.0},
            challenger_metrics={"mse": math.nan},
            provenance_complete=True,
            rollback_target="strategy-v1",
        )


def test_registry_backed_factory_vertical_retains_inconclusive_and_survives_restart(tmp_path):
    registry, registry_path, rule, store, evaluator_config, input_manifest = (
        _factory_foundation(tmp_path)
    )
    model_factory = _RecordingMeanFactory()
    runner = ExperimentRunner(
        registry, store, baseline_model_factory=model_factory
    )
    result = _run_candidate(runner, _candidate_points(), rule)

    assert result.verdict is PromotionVerdict.INCONCLUSIVE
    assert result.registry_action is PromotionAction.RETAIN
    assert result.candidate_metrics["max_squared_error"] <= 0.50
    assert model_factory.fit_ids[-1] == "model-v2"
    assert registry.get("ModelVersion", "model-v2") is not None
    assert registry.get("StrategyVersion", "strategy-v2") is not None
    assert registry.get("PromotionDecision", "promotion-v2") is not None

    evaluation = store.read("evaluation", "eval-v2")
    metrics = store.read("metrics", "eval-v2")
    model = store.read("model", "model-v2")
    assert evaluation["evaluator_config"] == evaluator_config.canonical_payload()
    assert evaluation["evaluator_config_sha256"] == evaluator_config.config_sha256
    assert evaluation["training_points_manifest_sha256"] == input_manifest
    assert metrics["training_points_manifest_sha256"] == input_manifest
    assert model["training_points_manifest_sha256"] == input_manifest
    assert evaluation["walk_forward"]["folds"][0]["target_available_at"] == T4
    assert evaluation["walk_forward"]["folds"][0]["causal_training_count"] == 2
    assert evaluation["walk_forward_result_sha256"] == _canonical_sha(
        evaluation["walk_forward"]
    )
    assert metrics["walk_forward_result_sha256"] == evaluation["walk_forward_result_sha256"]
    bundle_entry = registry.get("EvaluationBundle", "eval-v2")
    decision_entry = registry.get("PromotionDecision", "promotion-v2")
    assert bundle_entry is not None
    assert decision_entry is not None
    assert decision_entry.payload["action"] == PromotionAction.RETAIN.value
    evidence_entry = registry.get(
        "PromotionEvidence", decision_entry.payload["promotion_evidence_id"]
    )
    assert evidence_entry is not None
    effect_evidence = evaluation["promotion_effect_evidence"]
    assert effect_evidence["evaluation_bundle_id"] == "eval-v2"
    assert effect_evidence["holdout_consumed"] is True
    assert evidence_entry.payload["holdout_consumed"] is True
    assert "confirmation holdout already consumed" in decision_entry.payload["reason"]
    assert (
        effect_evidence["effective_sample_size"]
        == bundle_entry.payload["effective_sample_size"]
        == evidence_entry.payload["effective_sample_size"]
    )
    for field in (
        "effect_interval_low",
        "effect_interval_high",
        "practical_improvement",
    ):
        assert effect_evidence[field] == bundle_entry.payload[field]
        assert effect_evidence[field] == evidence_entry.payload[field]
    for field in (
        "holdout_access_id",
        "stopping_rule_sha256",
        "multiple_comparison_control_sha256",
        "uncertainty_method",
    ):
        assert effect_evidence[field] == evidence_entry.payload[field]

    restarted = ExperimentRunner.verify_restart(
        registry_path,
        tmp_path / "factory-artifacts",
        "experiment-v2",
        as_of=T7,
    )
    assert restarted.champion_strategy_version_id == "strategy-v1"
    assert restarted.outcome is ResearchOutcome.INCONCLUSIVE
    assert restarted.reproducibility_bundle_sha256 == result.reproducibility_bundle_sha256


def test_factory_fails_closed_on_frozen_promotion_rule_tampering(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    tampered = replace(rule, minimum_improvement=0.01)
    with pytest.raises(ValueError, match="does not match frozen"):
        _run_candidate(ExperimentRunner(registry, store), _candidate_points(), tampered)
    assert registry.get("ModelVersion", "model-v2") is None


def test_factory_fails_closed_on_runtime_evaluator_config_mutation(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    with pytest.raises(ValueError, match="does not match frozen evaluator config"):
        _run_candidate(
            ExperimentRunner(registry, store),
            _candidate_points(),
            rule,
            minimum_train_size=1,
        )
    assert registry.get("ModelVersion", "model-v2") is None
    assert registry.get("PromotionDecision", "promotion-v2") is None


def test_factory_fails_closed_on_same_version_different_feature_identity(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    registry.append(FeatureSet("features-rogue", "v1", SHA_A, SHA_D, T0))
    rogue_spec = replace(_candidate_spec(), feature_set_id="features-rogue")
    with pytest.raises(ValueError, match="feature set identity does not match frozen"):
        ExperimentRunner(registry, store).run_baseline_candidate(
            rogue_spec, _candidate_points(), rule=rule
        )
    assert registry.get("ModelVersion", "model-v2") is None
    assert registry.get("PromotionDecision", "promotion-v2") is None


def test_factory_fails_closed_on_altered_rows_under_same_dataset_identity(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    altered = list(_candidate_points())
    altered[-1] = replace(altered[-1], target=0.25)
    with pytest.raises(ValueError, match="do not match frozen DatasetSnapshot manifest"):
        _run_candidate(ExperimentRunner(registry, store), tuple(altered), rule)
    assert registry.get("ModelVersion", "model-v2") is None
    assert registry.get("PromotionDecision", "promotion-v2") is None
    assert not store.path_for_testing("evaluation", "eval-v2").exists()


def test_factory_fails_closed_when_champion_metrics_are_not_durably_bound(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    target = store.path_for_testing("metrics", "eval-v1")
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["metrics"]["mse"] = 99.0
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="not hash-bound"):
        _run_candidate(ExperimentRunner(registry, store), _candidate_points(), rule)
    assert registry.get("PromotionDecision", "promotion-v2") is None


def test_factory_rejects_mismatched_champion_evaluator_source_before_mutation(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    mismatched = replace(_candidate_spec(), evaluator_source_sha256=SHA_B)
    with pytest.raises(ValueError, match="evaluator source mismatch"):
        ExperimentRunner(registry, store).run_baseline_candidate(
            mismatched, _candidate_points(), rule=rule
        )
    assert registry.get("ModelVersion", "model-v2") is None


def test_factory_rejects_tampered_champion_evaluator_config_identity(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    target = store.path_for_testing("metrics", "eval-v1")
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["evaluator_config_sha256"] = SHA_A
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="not hash-bound|hash mismatch"):
        _run_candidate(ExperimentRunner(registry, store), _candidate_points(), rule)
    assert registry.get("PromotionDecision", "promotion-v2") is None


def test_factory_rejects_caller_injected_promotion_authority_metrics(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        ExperimentRunner(registry, store).run_baseline_candidate(
            _candidate_spec(),
            _candidate_points(),
            rule=rule,
            protective_metrics={"max_squared_error": 0.0},
        )


def test_factory_rejection_is_durable_negative_memory_with_postmortem(tmp_path):
    points = _bad_candidate_points()
    registry, registry_path, rule, store, _, _ = _factory_foundation(
        tmp_path, points=points
    )
    result = _run_candidate(ExperimentRunner(registry, store), points, rule)
    assert result.verdict is PromotionVerdict.REJECT
    reopened = ScientificRegistry(registry_path)
    experiment = reopened.get("Experiment", "experiment-v2")
    assert experiment.payload["outcome"] == "NEGATIVE"
    assert reopened.get("Postmortem", "experiment-v2:postmortem") is not None
    assert reopened.champion_strategy(
        as_of=T7, canonical_strategy_id="canonical-factory-strategy"
    ) == "strategy-v1"


def test_factory_duplicate_fingerprint_fails_before_new_durable_state(tmp_path):
    points = _bad_candidate_points()
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path, points=points)
    runner = ExperimentRunner(registry, store)
    assert _run_candidate(runner, points, rule).verdict is PromotionVerdict.REJECT
    duplicate = replace(
        _candidate_spec(),
        experiment_id="experiment-v2-duplicate",
        evaluation_bundle_id="eval-v2-duplicate",
        promotion_decision_id="promotion-v2-duplicate",
    )
    with pytest.raises(DuplicateExperimentFingerprintError):
        runner.run_baseline_candidate(duplicate, points, rule=rule)
    assert registry.get("Experiment", "experiment-v2-duplicate") is None
    assert not store.path_for_testing("evaluation", "eval-v2-duplicate").exists()


def test_protective_metric_degradation_is_durably_rejected(tmp_path):
    points = _guardrail_bad_candidate_points()
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path, points=points)
    result = _run_candidate(ExperimentRunner(registry, store), points, rule)
    assert result.candidate_metrics["mse"] < 0.75
    assert result.candidate_metrics["max_squared_error"] > 0.50
    assert result.verdict is PromotionVerdict.REJECT
    decision = registry.get("PromotionDecision", "promotion-v2")
    assert decision.payload["action"] == "REJECT"
    assert "protective metric degraded" in decision.payload["reason"]


def test_restart_detects_tampered_evaluation_artifact(tmp_path):
    registry, registry_path, rule, store, _, _ = _factory_foundation(tmp_path)
    _run_candidate(ExperimentRunner(registry, store), _candidate_points(), rule)
    target = store.path_for_testing("evaluation", "eval-v2")
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["candidate_metrics"]["mse"] = 999.0
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        ExperimentRunner.verify_restart(
            registry_path,
            tmp_path / "factory-artifacts",
            "experiment-v2",
            as_of=T7,
        )


def test_restart_detects_tampered_candidate_metrics_artifact(tmp_path):
    registry, registry_path, rule, store, _, _ = _factory_foundation(tmp_path)
    _run_candidate(ExperimentRunner(registry, store), _candidate_points(), rule)
    target = store.path_for_testing("metrics", "eval-v2")
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["metrics"]["mse"] = 999.0
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        ExperimentRunner.verify_restart(
            registry_path,
            tmp_path / "factory-artifacts",
            "experiment-v2",
            as_of=T7,
        )


def test_drift_monitor_only_emits_research_recommendations():
    recommendations = DriftMonitor.recommendations(
        (
            DriftEvidence("mse", 0.20, 0.21, 0.05, T4),
            DriftEvidence("calibration", 0.02, 0.20, 0.05, T4),
        )
    )
    assert recommendations == (f"RESEARCH_CHALLENGER:calibration:{T4}",)
    assert all("PROMOTE" not in item for item in recommendations)


def test_drift_evidence_is_causal_durable_and_has_no_promotion_authority(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    _run_candidate(ExperimentRunner(registry, store), _candidate_points(), rule)
    before = registry.champion_strategy(
        as_of=T7, canonical_strategy_id="canonical-factory-strategy"
    )
    drift = DriftMonitor.record_evidence(
        registry,
        store,
        evaluation_bundle_id="drift-v2",
        strategy_version_id="strategy-v2",
        model_version_id="model-v2",
        dataset_snapshot_id="dataset-factory",
        research_protocol_id="protocol-factory",
        evaluator_source_sha256=SHA_C,
        evidence=(DriftEvidence("mse", 0.30, 0.60, 0.10, T6),),
        recorded_at=T7,
    )
    assert drift.recommendations == (f"RESEARCH_CHALLENGER:mse:{T6}",)
    assert registry.get("PromotionDecision", "drift-v2") is None
    assert registry.champion_strategy(
        as_of=T7, canonical_strategy_id="canonical-factory-strategy"
    ) == before

    with pytest.raises(ValueError, match="not causally available"):
        DriftMonitor.record_evidence(
            registry,
            store,
            evaluation_bundle_id="drift-future",
            strategy_version_id="strategy-v2",
            model_version_id="model-v2",
            dataset_snapshot_id="dataset-factory",
            research_protocol_id="protocol-factory",
            evaluator_source_sha256=SHA_C,
            evidence=(DriftEvidence("mse", 0.30, 0.60, 0.10, "2026-01-09T00:00:00+00:00"),),
            recorded_at=T7,
        )


class _ForbiddenPredictionFieldModel:
    def __init__(self, model_id: str, training_cutoff: str, forbidden_field: str) -> None:
        self.model_id = model_id
        self.training_cutoff = training_cutoff
        self.forbidden_field = forbidden_field

    def predict(self, point, *, decision_at: str) -> float:
        return float(getattr(point, self.forbidden_field))


class _ForbiddenPredictionFieldFactory:
    model_family = "adversarial-target-reader-v1"

    def __init__(self, forbidden_field: str) -> None:
        self.forbidden_field = forbidden_field

    def fit(self, model_id, points, *, training_cutoff):
        return _ForbiddenPredictionFieldModel(
            model_id,
            training_cutoff,
            self.forbidden_field,
        )


@pytest.mark.parametrize("forbidden_field", ("target", "target_available_at"))
def test_walk_forward_plugin_cannot_read_evaluation_label(forbidden_field: str):
    with pytest.raises(AttributeError, match=forbidden_field):
        WalkForwardRunner.run(
            _points(),
            minimum_train_size=2,
            model_factory=_ForbiddenPredictionFieldFactory(forbidden_field),
        )

