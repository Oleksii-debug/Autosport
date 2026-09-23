import hashlib
import json

import pytest

from autosport.scientific_registry import (
    DatasetSnapshot,
    FeatureSet,
    Hypothesis,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
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


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T_BEFORE = "2025-12-31T00:00:00+00:00"
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"


def _payload_sha(record) -> str:
    canonical = json.dumps(
        record.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_factory_rejects_retrospective_preregistration_before_any_candidate_mutation(
    tmp_path,
):
    points = (
        TrainingPoint(T0, 1.0, 0.0, T0),
        TrainingPoint(T1, 2.0, 1.0, T1),
        TrainingPoint(T2, 3.0, 1.0, T2),
    )
    evaluator_config = WalkForwardEvaluationConfig(
        1,
        feature_set_id="features-retrospective",
        feature_definition_sha256=SHA_B,
        feature_source_sha256=SHA_C,
    )
    rule = PromotionRule("mse", 0.05)
    manifest_sha256 = training_points_manifest_sha256(points)

    question = ResearchQuestion(
        "question-retrospective",
        "Can a frozen causal baseline improve MSE?",
        SHA_A,
        T0,
    )
    hypothesis = Hypothesis(
        "hypothesis-retrospective",
        question.question_id,
        "The challenger lowers MSE.",
        "mse improves by the frozen threshold",
        "mse misses the frozen threshold",
        "mse",
        (),
        T0,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="protocol-retrospective",
        research_question_id=question.question_id,
        research_question_sha256=_payload_sha(question),
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=_payload_sha(hypothesis),
        inclusion_criteria="predeclared governed examples",
        exclusion_criteria="missing provenance or causal availability",
        lawful_source_requirements="lawful retained source evidence",
        causal_cutoff=T1,
        evaluation_design=evaluator_config.frozen_text,
        feature_set_version="v1",
        uncertainty_method="deterministic baseline checkpoint",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time order",),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=rule.frozen_text,
        expected_artifacts=("model artifact", "evaluation bundle", "promotion decision"),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, manifest_sha256, T0)
    dataset = DatasetSnapshot(
        "dataset-retrospective",
        manifest_sha256,
        "lawful-provider:fixture",
        "license-evidence:v1",
        T1,
        T0,
        outcome_reveal_after=T1,
    )
    feature_set = FeatureSet(
        "features-retrospective",
        "v1",
        SHA_B,
        SHA_C,
        T0,
    )

    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    for record in (question, hypothesis, protocol, dataset, feature_set):
        registry.append(record)
    store = FactoryArtifactStore(tmp_path / "factory-artifacts")
    runner = ExperimentRunner(registry, store)
    retrospective_spec = FactoryCandidateSpec(
        "experiment-retrospective",
        "model-retrospective",
        "strategy-retrospective",
        "eval-retrospective",
        "promotion-retrospective",
        "canonical-retrospective",
        protocol.record_id,
        dataset.dataset_snapshot_id,
        feature_set.feature_set_id,
        SHA_C,
        SHA_D,
        SHA_C,
        11,
        T_BEFORE,
        T3,
        T4,
        None,
        None,
    )

    with pytest.raises(
        ValueError,
        match="scientific foundation was not available by experiment start",
    ):
        runner.run_baseline_candidate(retrospective_spec, points, rule=rule)

    assert registry.get("ModelVersion", "model-retrospective") is None
    assert registry.get("StrategyVersion", "strategy-retrospective") is None
    assert registry.get("EvaluationBundle", "eval-retrospective") is None
    assert registry.get("Experiment", "experiment-retrospective") is None
    assert registry.get("PromotionDecision", "promotion-retrospective") is None
    assert not store.path_for_testing("model", "model-retrospective").exists()
    assert not store.path_for_testing("evaluation", "eval-retrospective").exists()
