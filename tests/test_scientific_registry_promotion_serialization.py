import hashlib
import json
from dataclasses import replace

import pytest

from test_scientific_registry import _frozen_promotion_rule_text, _promotion_evidence

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


def _seed_promotion_evidence(
    registry: ScientificRegistry,
    *,
    frozen_feature_version: str = "v1",
    frozen_config_sha256: str = SHA_B,
    dataset_causal_cutoff: str = T1,
    eval1_strategy_id: str = "strategy-1",
    eval1_model_id: str | None = "model-1",
) -> tuple[ResearchProtocol, EvaluationBundleRef, EvaluationBundleRef, ExperimentRecord]:
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
        feature_set_version=frozen_feature_version,
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split",),
        random_seed_policy="seed frozen before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=_frozen_promotion_rule_text(),
        expected_artifacts=("evaluation bundle", "decision"),
        code_config_sha256=frozen_config_sha256,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, SHA_A, T0)
    dataset = DatasetSnapshot(
        "dataset-1",
        SHA_A,
        "lawful-provider:fixture",
        "license-evidence:v1",
        dataset_causal_cutoff,
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
        "strategy-2", "canonical-strategy", SHA_C, SHA_D, SHA_B, T1, model_version_id="model-1"
    )
    eval1 = EvaluationBundleRef(
        "eval-1",
        SHA_D,
        SHA_C,
        "dataset-1",
        protocol.protocol_sha256,
        (SHA_A,),
        T2,
        evaluated_strategy_version_id=eval1_strategy_id,
        evaluated_model_version_id=eval1_model_id,
        effective_sample_size=5,
        effect_interval_low="0.05",
        effect_interval_high="0.15",
        practical_improvement="0.1",
    )
    eval2 = EvaluationBundleRef(
        "eval-2",
        SHA_A,
        SHA_C,
        "dataset-1",
        protocol.protocol_sha256,
        (SHA_B,),
        T2,
        evaluated_strategy_version_id="strategy-2",
        evaluated_model_version_id="model-1",
        effective_sample_size=5,
        effect_interval_low="0.05",
        effect_interval_high="0.15",
        practical_improvement="0.1",
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
        SHA_B,
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
    evidence1 = _promotion_evidence(
        experiment_id="experiment-1", strategy_id="strategy-1", model_id="model-1",
        bundle_id="eval-1", dataset_id="dataset-1", protocol_id="protocol-1",
        bundle_sha=eval1.bundle_sha256, evidence_id="promotion-1-evidence",
        rollback_identity="NONE", minimum_n=3,
    )
    registry.append(evidence1)
    return protocol, eval1, eval2, exp1, evidence1.promotion_evidence_id


def _first_promotion(
    protocol: ResearchProtocol,
    bundle: EvaluationBundleRef,
    promotion_evidence_id: str,
) -> PromotionDecision:
    return PromotionDecision(
        "promotion-1",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        protocol.protocol_sha256,
        "eval-1",
        bundle.bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=promotion_evidence_id,
    )

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
    protocol, eval1, eval2, _, evidence_id = _seed_promotion_evidence(registry)
    registry.record_promotion(_first_promotion(protocol, eval1, evidence_id))

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
    with pytest.raises(PromotionEvidenceError, match="current context champion"):
        registry.record_promotion(conflicting)

    assert registry.champion_strategy(as_of=T3) == "strategy-1"


def test_promotion_decision_cannot_be_backdated_before_durable_history(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, eval1, _, _, evidence_id = _seed_promotion_evidence(registry)
    registry.record_promotion(_first_promotion(protocol, eval1, evidence_id))

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


def test_equal_instant_promotion_cannot_reorder_before_durable_history(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, eval1, eval2, _, evidence_id = _seed_promotion_evidence(registry)
    registry.record_promotion(_first_promotion(protocol, eval1, evidence_id))

    earlier_total_order = PromotionDecision(
        "promotion-0",
        PromotionAction.PROMOTE,
        "strategy-2",
        "protocol-1",
        protocol.protocol_sha256,
        "eval-2",
        eval2.bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        predecessor_strategy_version_id="strategy-1",
    )
    with pytest.raises(PromotionEvidenceError, match="backdated"):
        registry.record_promotion(earlier_total_order)

    assert registry.champion_strategy(as_of=T3) == "strategy-1"


def test_promotion_rejects_dataset_causal_cutoff_outside_frozen_protocol(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, eval1, _, _, evidence_id = _seed_promotion_evidence(
        registry,
        dataset_causal_cutoff=T2,
    )

    with pytest.raises(PromotionEvidenceError, match="causal cutoff"):
        registry.record_promotion(_first_promotion(protocol, eval1, evidence_id))


def test_rollback_rejects_existing_strategy_that_was_never_champion(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, eval1, _, _, evidence_id = _seed_promotion_evidence(registry)
    registry.record_promotion(_first_promotion(protocol, eval1, evidence_id))

    unrelated = PromotionDecision(
        "rollback-unrelated",
        PromotionAction.ROLLBACK,
        "strategy-1",
        "protocol-1",
        protocol.protocol_sha256,
        "eval-1",
        eval1.bundle_sha256,
        T3,
        rollback_to_strategy_version_id="strategy-2",
        candidate_model_version_id="model-1",
    )
    with pytest.raises(PromotionEvidenceError, match="rollback target"):
        registry.record_promotion(unrelated)

    assert registry.champion_strategy(as_of=T3) == "strategy-1"


def test_promotion_rejects_feature_version_outside_frozen_protocol(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, eval1, _, _, evidence_id = _seed_promotion_evidence(
        registry,
        frozen_feature_version="v2",
    )

    with pytest.raises(PromotionEvidenceError, match="feature set version"):
        registry.record_promotion(_first_promotion(protocol, eval1, evidence_id))


def test_promotion_rejects_config_outside_frozen_protocol(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, eval1, _, _, evidence_id = _seed_promotion_evidence(
        registry,
        frozen_config_sha256=SHA_C,
    )

    with pytest.raises(PromotionEvidenceError, match="config does not match frozen"):
        registry.record_promotion(_first_promotion(protocol, eval1, evidence_id))


def test_promotion_rejects_bundle_candidate_identity_mismatch(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, eval1, _, _, evidence_id = _seed_promotion_evidence(
        registry,
        eval1_strategy_id="strategy-2",
    )

    with pytest.raises(PromotionEvidenceError, match="bundle strategy identity"):
        registry.record_promotion(_first_promotion(protocol, eval1, evidence_id))


def test_promotion_rejects_ambiguous_duplicate_matching_experiment(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol, eval1, _, exp1, evidence_id = _seed_promotion_evidence(registry)
    registry.append(
        replace(exp1, experiment_id="experiment-repeat"),
        allow_repeat_experiment=True,
    )

    with pytest.raises(PromotionEvidenceError, match="ambiguous"):
        registry.record_promotion(_first_promotion(protocol, eval1, evidence_id))
