import hashlib
import json

import pytest

from autosport.scientific_registry import (
    ConflictingScientificRecordError,
    DatasetSnapshot,
    EvaluationBundleRef,
    ExperimentRecord,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    PromotionAction,
    PromotionDecision,
    PromotionEvidenceError,
    ResearchOutcome,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
    StrategyVersion,
)
from autosport.strategy_experiment import ScientificProtocolBinding


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"


def _payload_sha(record) -> str:
    canonical = json.dumps(
        record.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _seed_promotion_evidence(registry: ScientificRegistry) -> tuple[ResearchProtocol, EvaluationBundleRef, EvaluationBundleRef]:
    question = ResearchQuestion("question-1", "Does the candidate improve the frozen metric?", SHA_A, T0)
    hypothesis = Hypothesis(
        "hypothesis-1",
        "question-1",
        "Candidate improves the frozen metric.",
        "primary metric improves",
        "primary metric does not improve",
        "roi",
        ("drawdown",),
        T0,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="protocol-1",
        research_question_id=question.question_id,
        research_question_sha256=_payload_sha(question),
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=_payload_sha(hypothesis),
        inclusion_criteria="predeclared events",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="retained lawful source evidence",
        causal_cutoff=T1,
        evaluation_design="sealed walk-forward holdout",
        feature_set_version="features-v1",
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen metric",
        robustness_checks=("time split",),
        random_seed_policy="seed frozen before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule="promote only on positive frozen outcome",
        expected_artifacts=("evaluation bundle", "decision"),
        code_config_sha256=SHA_C,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, SHA_A, T0)
    dataset = DatasetSnapshot(
        "dataset-1",
        SHA_A,
        "lawful-provider:fixture",
        "license-evidence:v1",
        T1,
        T0,
        outcome_reveal_after=T1,
    )
    features = FeatureSet("features-1", "v1", SHA_B, SHA_C, T0)
    model = ModelVersion(
        "model-1",
        "fixture-model",
        SHA_A,
        SHA_C,
        SHA_D,
        "dataset-1",
        "features-1",
        "protocol-1",
        7,
        SHA_B,
        T1,
    )
    strategy1 = StrategyVersion(
        "strategy-1", "canonical-strategy", SHA_C, SHA_D, SHA_B, T1, model_version_id="model-1"
    )
    strategy2 = StrategyVersion(
        "strategy-2", "canonical-strategy", SHA_C, SHA_D, SHA_A, T1, model_version_id="model-1"
    )
    eval1 = EvaluationBundleRef(
        "eval-1", SHA_D, SHA_C, "dataset-1", protocol.protocol_sha256, (SHA_A,), T2
    )
    eval2 = EvaluationBundleRef(
        "eval-2", SHA_A, SHA_C, "dataset-1", protocol.protocol_sha256, (SHA_B,), T2
    )
    exp1 = ExperimentRecord(
        "experiment-1",
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-1",
        "eval-1",
        7,
        SHA_B,
        ResearchOutcome.POSITIVE,
        T1,
        model_version_id="model-1",
        completed_at=T2,
    )
    exp2 = ExperimentRecord(
        "experiment-2",
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-2",
        "eval-2",
        7,
        SHA_C,
        ResearchOutcome.POSITIVE,
        T1,
        model_version_id="model-1",
        completed_at=T2,
    )
    for record in (
        question,
        hypothesis,
        protocol,
        dataset,
        features,
        model,
        strategy1,
        strategy2,
        eval1,
        eval2,
        exp1,
        exp2,
    ):
        registry.append(record)
    return protocol, eval1, eval2


def test_evaluation_bundle_cannot_be_rebound_to_different_experiment_lineage(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    first = ExperimentRecord(
        "experiment-1",
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-1",
        "eval-1",
        7,
        SHA_A,
        ResearchOutcome.NULL,
        T1,
        model_version_id="model-1",
        completed_at=T2,
    )
    second = ExperimentRecord(
        "experiment-2",
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-2",
        "eval-1",
        7,
        SHA_A,
        ResearchOutcome.NULL,
        T1,
        model_version_id="model-1",
        completed_at=T2,
    )
    registry.append(first)

    with pytest.raises(ConflictingScientificRecordError, match="evaluation bundle is already bound"):
        registry.append(second)


def test_second_promotion_must_name_current_durable_champion(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, eval1, eval2 = _seed_promotion_evidence(registry)

    first = PromotionDecision(
        "promotion-1",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        protocol.protocol_sha256,
        "eval-1",
        eval1.bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
    )
    registry.record_promotion(first)

    conflicting = PromotionDecision(
        "promotion-2",
        PromotionAction.PROMOTE,
        "strategy-2",
        "protocol-1",
        protocol.protocol_sha256,
        "eval-2",
        eval2.bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
    )
    with pytest.raises(PromotionEvidenceError, match="current champion"):
        registry.record_promotion(conflicting)

    assert registry.champion_strategy(as_of=T3) == "strategy-1"


def test_promotion_decision_cannot_be_backdated_before_durable_history(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, eval1, _ = _seed_promotion_evidence(registry)
    first = PromotionDecision(
        "promotion-1",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        protocol.protocol_sha256,
        "eval-1",
        eval1.bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
    )
    registry.record_promotion(first)

    backdated = PromotionDecision(
        "promotion-backdated",
        PromotionAction.RETAIN,
        "strategy-1",
        "protocol-1",
        protocol.protocol_sha256,
        "eval-1",
        eval1.bundle_sha256,
        T2,
        candidate_model_version_id="model-1",
    )
    with pytest.raises(PromotionEvidenceError, match="backdated"):
        registry.record_promotion(backdated)
