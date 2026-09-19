import hashlib
import json

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
    promotion_holdout_access_id,
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


def test_same_instant_lexically_earlier_promotion_is_rejected_before_publication(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    question = ResearchQuestion("question-1", "Frozen question", SHA_A, T0)
    hypothesis = Hypothesis(
        "hypothesis-1",
        "question-1",
        "Frozen hypothesis",
        "metric improves",
        "metric does not improve",
        "roi",
        (),
        T0,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="protocol-1",
        research_question_id="question-1",
        research_question_sha256=_payload_sha(question),
        hypothesis_id="hypothesis-1",
        hypothesis_sha256=_payload_sha(hypothesis),
        inclusion_criteria="frozen inclusion",
        exclusion_criteria="frozen exclusion",
        lawful_source_requirements="lawful source evidence",
        causal_cutoff=T1,
        evaluation_design="sealed holdout",
        feature_set_version="v1",
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single metric",
        robustness_checks=("time split",),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=_frozen_promotion_rule_text(),
        expected_artifacts=("evaluation bundle",),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, SHA_A, T0)
    dataset = DatasetSnapshot(
        "dataset-1", SHA_A, "source", "license", T1, T0, outcome_reveal_after=T1
    )
    features = FeatureSet("features-1", "v1", SHA_A, SHA_C, T0)
    model = ModelVersion(
        "model-1", "fixture", SHA_A, SHA_C, SHA_D, "dataset-1", "features-1",
        "protocol-1", 7, SHA_B, T1
    )
    strategy1 = StrategyVersion(
        "strategy-1", "canonical", SHA_C, SHA_D, SHA_B, T1, model_version_id="model-1"
    )
    strategy2 = StrategyVersion(
        "strategy-2", "canonical", SHA_C, SHA_D, SHA_B, T1,
        model_version_id="model-1", predecessor_strategy_version_id="strategy-1"
    )
    bundle1 = EvaluationBundleRef(
        "eval-1", SHA_C, SHA_C, "dataset-1", protocol.protocol_sha256, (SHA_A,), T2,
        evaluated_strategy_version_id="strategy-1", evaluated_model_version_id="model-1",
        effective_sample_size=5,
        effect_interval_low="0.05", effect_interval_high="0.15", practical_improvement="0.1"
    )
    bundle2 = EvaluationBundleRef(
        "eval-2", SHA_D, SHA_C, "dataset-1", protocol.protocol_sha256, (SHA_B,), T2,
        evaluated_strategy_version_id="strategy-2", evaluated_model_version_id="model-1",
        effective_sample_size=5,
        effect_interval_low="0.05", effect_interval_high="0.15", practical_improvement="0.1"
    )
    experiment1 = ExperimentRecord(
        "experiment-1", "protocol-1", "dataset-1", "features-1", "strategy-1", "eval-1",
        7, SHA_B, ResearchOutcome.POSITIVE, T1, model_version_id="model-1", completed_at=T2
    )
    experiment2 = ExperimentRecord(
        "experiment-2", "protocol-1", "dataset-1", "features-1", "strategy-2", "eval-2",
        7, SHA_B, ResearchOutcome.POSITIVE, T1, model_version_id="model-1", completed_at=T2
    )
    for record in (
        question, hypothesis, protocol, dataset, features, model, strategy1, strategy2,
        bundle1, bundle2, experiment1, experiment2,
    ):
        registry.append(record)

    evidence1 = _promotion_evidence(
        experiment_id="experiment-1", strategy_id="strategy-1", model_id="model-1",
        bundle_id="eval-1", dataset_id="dataset-1", protocol_id="protocol-1",
        bundle_sha=bundle1.bundle_sha256, evidence_id="promotion-1-evidence",
        rollback_identity="NONE", minimum_n=3,
        holdout_access_id=promotion_holdout_access_id(
            research_protocol_id="protocol-1",
            dataset_manifest_sha256=SHA_A,
            source_identity="source",
            license_identity="license",
            confirmation_trial_family_id="protocol-1:confirmation-trial-family",
        ),
    )
    registry.append(evidence1)
    first = PromotionDecision(
        "z-promotion", PromotionAction.PROMOTE, "strategy-1", "protocol-1",
        protocol.protocol_sha256, "eval-1", bundle1.bundle_sha256, T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence1.promotion_evidence_id
    )
    registry.record_promotion(first)

    replay_inverting = PromotionDecision(
        "a-promotion", PromotionAction.RETAIN, "strategy-2", "protocol-1",
        protocol.protocol_sha256, "eval-2", bundle2.bundle_sha256, T3,
        predecessor_strategy_version_id="strategy-1", candidate_model_version_id="model-1"
    )
    with pytest.raises(PromotionEvidenceError, match="backdated"):
        registry.record_promotion(replay_inverting)

    assert registry.get("PromotionDecision", "a-promotion") is None
    assert registry.champion_strategy(as_of=T3) == "strategy-1"
