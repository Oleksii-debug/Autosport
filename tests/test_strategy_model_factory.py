import hashlib
import json
import math
from dataclasses import replace

import pytest

from autosport.scientific_registry import (
    DatasetSnapshot,
    EvaluationBundleRef,
    ExperimentRecord,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    PromotionAction,
    PromotionDecision,
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
    WalkForwardRunner,
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
        TrainingPoint("2026-01-01T00:00:00+00:00", 1.0, 0.0),
        TrainingPoint("2026-01-02T00:00:00+00:00", 2.0, 1.0),
        TrainingPoint("2026-01-03T00:00:00+00:00", 3.0, 1.0),
        TrainingPoint("2026-01-04T00:00:00+00:00", 4.0, 0.0),
    )


def _payload_sha(record) -> str:
    canonical = json.dumps(
        record.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _factory_rule() -> PromotionRule:
    return PromotionRule("mse", 0.05, (("max_drawdown", 0.20),))


def _factory_foundation(tmp_path):
    rule = _factory_rule()
    registry_path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(registry_path)
    question = ResearchQuestion(
        "question-factory",
        "Does the challenger lower frozen holdout MSE without guardrail regression?",
        SHA_A,
        T0,
    )
    hypothesis = Hypothesis(
        "hypothesis-factory",
        "question-factory",
        "Challenger lowers MSE while max drawdown remains bounded.",
        "mse improves by the frozen threshold",
        "mse misses threshold or max_drawdown exceeds guardrail",
        "mse",
        ("max_drawdown",),
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
        evaluation_design="causal expanding-window holdout",
        feature_set_version="v1",
        uncertainty_method="deterministic baseline checkpoint",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time order", "protective metric"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=rule.frozen_text,
        expected_artifacts=("model artifact", "evaluation bundle", "promotion decision"),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, SHA_A, T0)
    dataset = DatasetSnapshot(
        "dataset-factory",
        SHA_A,
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
    champion_bundle = EvaluationBundleRef(
        "eval-v1",
        SHA_D,
        SHA_C,
        dataset.dataset_snapshot_id,
        protocol.protocol_sha256,
        (SHA_A,),
        T3,
        evaluated_strategy_version_id=champion_strategy.strategy_version_id,
        evaluated_model_version_id=champion_model.model_version_id,
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
    registry.record_promotion(
        PromotionDecision(
            "promotion-v1",
            PromotionAction.PROMOTE,
            champion_strategy.strategy_version_id,
            binding.research_protocol_id,
            protocol.protocol_sha256,
            champion_bundle.evaluation_bundle_id,
            SHA_D,
            T3,
            candidate_model_version_id=champion_model.model_version_id,
            reason="fixture baseline champion",
        )
    )
    return registry, registry_path, rule


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


def _candidate_points():
    return (
        TrainingPoint(T0, 1.0, 0.0, T0),
        TrainingPoint(T1, 2.0, 1.0, T1),
        TrainingPoint(T4, 3.0, 1.0, T4),
        TrainingPoint(T5, 4.0, 0.0, T5),
    )


def test_mean_baseline_uses_only_observations_at_or_before_training_cutoff():
    model = MeanBaselineModel.fit(
        "baseline-1", _points(), training_cutoff="2026-01-02T00:00:00+00:00"
    )
    assert model.training_count == 2
    assert model.mean_target == 0.5
    assert len(model.identity_sha256) == 64


def test_mean_baseline_rejects_future_prediction_input():
    model = MeanBaselineModel.fit(
        "baseline-1", _points()[:2], training_cutoff="2026-01-02T00:00:00+00:00"
    )
    with pytest.raises(ValueError, match="not available"):
        model.predict(
            TrainingPoint("2026-01-04T00:00:00+00:00", 4.0, 0.0),
            decision_at="2026-01-03T00:00:00+00:00",
        )


def test_baseline_excludes_labels_not_revealed_by_training_cutoff():
    points = (
        TrainingPoint(T0, 1.0, 0.0, T4),
        TrainingPoint(T1, 2.0, 1.0, T1),
    )
    model = MeanBaselineModel.fit("baseline-delayed", points, training_cutoff=T2)
    assert model.training_count == 1
    assert model.mean_target == 1.0


def test_walk_forward_is_expanding_window_and_deterministic():
    first = WalkForwardRunner.run(_points(), minimum_train_size=2)
    second = WalkForwardRunner.run(tuple(reversed(_points())), minimum_train_size=2)
    assert first == second
    assert first.result_sha256 == second.result_sha256
    assert [fold.training_cutoff for fold in first.folds] == [
        "2026-01-02T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
    ]
    assert all(fold.training_cutoff < fold.evaluation_at for fold in first.folds)
    assert first.primary_metric == "mse"


def test_walk_forward_rejects_duplicate_timestamp_identity():
    points = _points()[:2] + (
        TrainingPoint("2026-01-02T00:00:00+00:00", 9.0, 0.0),
    )
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
    assert accepted.verdict is PromotionVerdict.PROMOTE
    assert accepted.registry_action is PromotionAction.PROMOTE

    degraded = PromotionController.evaluate(
        rule,
        champion_metrics={"mse": 0.40, "max_drawdown": 0.10},
        challenger_metrics={"mse": 0.20, "max_drawdown": 0.25},
        provenance_complete=True,
        rollback_target="strategy-v1",
    )
    assert degraded.verdict is PromotionVerdict.REJECT
    assert degraded.registry_action is PromotionAction.REJECT
    assert "protective metric degraded: max_drawdown" in degraded.reasons

    incomplete = PromotionController.evaluate(
        rule,
        champion_metrics={"mse": 0.40},
        challenger_metrics={"mse": 0.20},
        provenance_complete=False,
        rollback_target="strategy-v1",
    )
    assert incomplete.verdict is PromotionVerdict.REJECT


def test_factory_rejects_nonfinite_metrics():
    rule = PromotionRule("mse", 0.0)
    with pytest.raises(ValueError, match="finite"):
        PromotionController.evaluate(
            rule,
            champion_metrics={"mse": 1.0},
            challenger_metrics={"mse": math.nan},
            provenance_complete=True,
            rollback_target="strategy-v1",
        )


def test_drift_monitor_only_emits_research_recommendations():
    recommendations = DriftMonitor.recommendations(
        (
            DriftEvidence("mse", 0.20, 0.21, 0.05, "2026-01-05T00:00:00+00:00"),
            DriftEvidence("calibration", 0.02, 0.20, 0.05, "2026-01-05T00:00:00+00:00"),
        )
    )
    assert recommendations == (
        "RESEARCH_CHALLENGER:calibration:2026-01-05T00:00:00+00:00",
    )
    assert all("PROMOTE" not in item for item in recommendations)


def test_registry_backed_factory_vertical_promotes_and_survives_restart(tmp_path):
    registry, registry_path, rule = _factory_foundation(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    runner = ExperimentRunner(registry, store)

    result = runner.run_baseline_candidate(
        _candidate_spec(),
        _candidate_points(),
        rule=rule,
        champion_metrics={"mse": 0.80, "max_drawdown": 0.10},
        protective_metrics={"max_drawdown": 0.10},
    )

    assert result.verdict is PromotionVerdict.PROMOTE
    assert registry.get("ModelVersion", "model-v2") is not None
    assert registry.get("StrategyVersion", "strategy-v2") is not None
    assert registry.get("EvaluationBundle", "eval-v2") is not None
    assert registry.get("Experiment", "experiment-v2").payload["outcome"] == "POSITIVE"
    assert registry.get("PromotionDecision", "promotion-v2") is not None
    assert registry.champion_strategy(
        as_of=T7, canonical_strategy_id="canonical-factory-strategy"
    ) == "strategy-v2"

    restarted = ExperimentRunner.verify_restart(
        registry_path,
        tmp_path / "factory-artifacts",
        "experiment-v2",
        as_of=T7,
    )
    assert restarted.champion_strategy_version_id == "strategy-v2"
    assert restarted.outcome is ResearchOutcome.POSITIVE
    assert restarted.reproducibility_bundle_sha256 == result.reproducibility_bundle_sha256


def test_factory_fails_closed_on_frozen_promotion_rule_tampering(tmp_path):
    registry, _, rule = _factory_foundation(tmp_path)
    runner = ExperimentRunner(
        registry, FactoryArtifactStore(tmp_path / "factory-artifacts")
    )
    tampered = replace(rule, minimum_improvement=0.01)
    with pytest.raises(ValueError, match="does not match frozen"):
        runner.run_baseline_candidate(
            _candidate_spec(),
            _candidate_points(),
            rule=tampered,
            champion_metrics={"mse": 0.80, "max_drawdown": 0.10},
            protective_metrics={"max_drawdown": 0.10},
        )
    assert registry.get("ModelVersion", "model-v2") is None


def test_factory_rejection_is_durable_negative_memory_with_postmortem(tmp_path):
    registry, registry_path, rule = _factory_foundation(tmp_path)
    runner = ExperimentRunner(
        registry, FactoryArtifactStore(tmp_path / "factory-artifacts")
    )
    result = runner.run_baseline_candidate(
        _candidate_spec(),
        _candidate_points(),
        rule=rule,
        champion_metrics={"mse": 0.20, "max_drawdown": 0.10},
        protective_metrics={"max_drawdown": 0.10},
    )
    assert result.verdict is PromotionVerdict.REJECT
    reopened = ScientificRegistry(registry_path)
    experiment = reopened.get("Experiment", "experiment-v2")
    assert experiment.payload["outcome"] == "NEGATIVE"
    assert reopened.get("Postmortem", "experiment-v2:postmortem") is not None
    assert reopened.find_experiment_fingerprint(experiment.payload["fingerprint"])
    assert reopened.champion_strategy(
        as_of=T7, canonical_strategy_id="canonical-factory-strategy"
    ) == "strategy-v1"


def test_restart_detects_tampered_evaluation_artifact(tmp_path):
    registry, registry_path, rule = _factory_foundation(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    ExperimentRunner(registry, store).run_baseline_candidate(
        _candidate_spec(),
        _candidate_points(),
        rule=rule,
        champion_metrics={"mse": 0.80, "max_drawdown": 0.10},
        protective_metrics={"max_drawdown": 0.10},
    )
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


def test_drift_evidence_is_causal_durable_and_has_no_promotion_authority(tmp_path):
    registry, _, rule = _factory_foundation(tmp_path)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    ExperimentRunner(registry, store).run_baseline_candidate(
        _candidate_spec(),
        _candidate_points(),
        rule=rule,
        champion_metrics={"mse": 0.80, "max_drawdown": 0.10},
        protective_metrics={"max_drawdown": 0.10},
    )
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
    assert registry.get("EvaluationBundle", "drift-v2") is not None
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
