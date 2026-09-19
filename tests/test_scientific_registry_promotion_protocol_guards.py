import hashlib
import json
from dataclasses import replace

import pytest

from test_scientific_registry import _frozen_promotion_rule_text, _promotion_evidence

from autosport.scientific_registry import (
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


def _seed_foundation(registry: ScientificRegistry, *, dataset_cutoff: str = T1):
    question = ResearchQuestion("question-1", "Does candidate improve holdout ROI?", SHA_A, T0)
    hypothesis = Hypothesis(
        "hypothesis-1",
        "question-1",
        "Candidate improves the frozen primary metric.",
        "holdout ROI > champion ROI",
        "holdout ROI <= champion ROI or any guardrail regresses",
        "roi",
        ("max_drawdown",),
        T0,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="protocol-1",
        research_question_id="question-1",
        research_question_sha256=_payload_sha(question),
        hypothesis_id="hypothesis-1",
        hypothesis_sha256=_payload_sha(hypothesis),
        inclusion_criteria="predeclared events",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="retained lawful source evidence",
        causal_cutoff=T1,
        evaluation_design="sealed walk-forward holdout",
        feature_set_version="v1",
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split", "source split"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=_frozen_promotion_rule_text(),
        expected_artifacts=("evaluation bundle", "decision"),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, SHA_A, T0)
    dataset = DatasetSnapshot(
        "dataset-1",
        SHA_A,
        "lawful-provider:fixture",
        "license-evidence:v1",
        dataset_cutoff,
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
    strategy = StrategyVersion(
        "strategy-1",
        "canonical-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T1,
        model_version_id="model-1",
    )
    bundle = EvaluationBundleRef(
        "eval-1",
        SHA_D,
        SHA_C,
        "dataset-1",
        protocol.protocol_sha256,
        (SHA_A, SHA_B),
        T2,
        evaluated_strategy_version_id="strategy-1",
        evaluated_model_version_id="model-1",
        effective_sample_size=5,
        effect_interval_low="0.05",
        effect_interval_high="0.15",
        practical_improvement="0.1",
    )
    experiment = ExperimentRecord(
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
    for record in (question, hypothesis, protocol, dataset, features, model, strategy, bundle, experiment):
        registry.append(record)
    evidence = _promotion_evidence(
        experiment_id="experiment-1", strategy_id="strategy-1", model_id="model-1",
        bundle_id="eval-1", dataset_id="dataset-1", protocol_id="protocol-1",
        bundle_sha=bundle.bundle_sha256, evidence_id="promotion-1-evidence",
        rollback_identity="NONE", minimum_n=3,
    )
    registry.append(evidence)
    return protocol, model, experiment


def _promotion(protocol: ResearchProtocol, *, decision_id: str = "promotion-1") -> PromotionDecision:
    return PromotionDecision(
        decision_id,
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        protocol.protocol_sha256,
        "eval-1",
        SHA_D,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id="promotion-1-evidence",
    )


def test_promotion_rejects_dataset_cutoff_different_from_frozen_protocol(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, _, _ = _seed_foundation(registry, dataset_cutoff=T2)

    with pytest.raises(PromotionEvidenceError, match="causal cutoff"):
        registry.record_promotion(_promotion(protocol))

    assert registry.get("PromotionDecision", "promotion-1") is None


def test_rollback_rejects_existing_strategy_that_was_never_a_champion(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, model, first_experiment = _seed_foundation(registry)
    registry.record_promotion(_promotion(protocol))

    strategy2 = StrategyVersion(
        "strategy-2",
        "canonical-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T3,
        model_version_id="model-1",
        predecessor_strategy_version_id="strategy-1",
    )
    bundle2 = EvaluationBundleRef(
        "eval-2",
        SHA_A,
        SHA_C,
        "dataset-1",
        protocol.protocol_sha256,
        (SHA_B,),
        T3,
        evaluated_strategy_version_id="strategy-2",
        evaluated_model_version_id="model-1",
    )
    experiment2 = replace(
        first_experiment,
        experiment_id="experiment-2",
        strategy_version_id="strategy-2",
        evaluation_bundle_id="eval-2",
        completed_at=T3,
    )
    unrelated = StrategyVersion(
        "strategy-unrelated",
        "canonical-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T3,
        model_version_id=model.model_version_id,
    )
    for record in (strategy2, bundle2, experiment2, unrelated):
        registry.append(record)

    registry.record_promotion(
        PromotionDecision(
            "promotion-2",
            PromotionAction.PROMOTE,
            "strategy-2",
            "protocol-1",
            protocol.protocol_sha256,
            "eval-2",
            SHA_A,
            T3,
            predecessor_strategy_version_id="strategy-1",
            candidate_model_version_id="model-1",
        )
    )
    rollback = PromotionDecision(
        "rollback-3",
        PromotionAction.ROLLBACK,
        "strategy-2",
        "protocol-1",
        protocol.protocol_sha256,
        "eval-2",
        SHA_A,
        T3,
        rollback_to_strategy_version_id="strategy-unrelated",
        candidate_model_version_id="model-1",
    )

    with pytest.raises(PromotionEvidenceError, match="prior durable champion"):
        registry.record_promotion(rollback)

    assert registry.champion_strategy(as_of=T3) == "strategy-2"
    assert registry.get("PromotionDecision", "rollback-3") is None
