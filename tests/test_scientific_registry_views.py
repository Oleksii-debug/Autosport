import hashlib
import json

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
    ScientificRegistryError,
    StrategyVersion,
)
from autosport.scientific_registry_views import (
    StrategyLifecycleState,
    strategy_lineage_projection,
    strategy_state_projection,
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
T4 = "2026-01-05T00:00:00+00:00"


def _payload_sha(record) -> str:
    canonical = json.dumps(
        record.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _seed_two_strategies(registry: ScientificRegistry):
    question = ResearchQuestion(
        "question-1",
        "Does the canonical strategy improve the frozen metric?",
        SHA_A,
        T0,
    )
    hypothesis = Hypothesis(
        "hypothesis-1",
        "question-1",
        "A challenger can improve the frozen metric.",
        "holdout ROI improves",
        "holdout ROI does not improve",
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
        feature_set_version="v1",
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen metric",
        robustness_checks=("time split",),
        random_seed_policy="seed frozen before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule="promote only on positive frozen outcome",
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
        "strategy-1",
        "predictive-edge:soccer:match-winner",
        SHA_C,
        SHA_D,
        SHA_B,
        T1,
        model_version_id="model-1",
    )
    strategy2 = StrategyVersion(
        "strategy-2",
        "predictive-edge:soccer:match-winner",
        SHA_C,
        SHA_D,
        SHA_B,
        T2,
        model_version_id="model-1",
        predecessor_strategy_version_id="strategy-1",
    )
    eval1 = EvaluationBundleRef(
        "eval-1",
        SHA_D,
        SHA_C,
        "dataset-1",
        protocol.protocol_sha256,
        (SHA_A,),
        T2,
        evaluated_strategy_version_id="strategy-1",
        evaluated_model_version_id="model-1",
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
        T2,
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


def test_strategy_state_projection_is_causal_and_restart_stable(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    protocol, eval1, eval2 = _seed_two_strategies(registry)
    key = "predictive-edge:soccer:match-winner"

    before = strategy_state_projection(registry, key, as_of=T2)
    assert [(item.strategy_version_id, item.state) for item in before] == [
        ("strategy-1", StrategyLifecycleState.CANDIDATE),
        ("strategy-2", StrategyLifecycleState.CANDIDATE),
    ]

    registry.record_promotion(
        PromotionDecision(
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
    )
    registry.record_promotion(
        PromotionDecision(
            "retain-2",
            PromotionAction.RETAIN,
            "strategy-2",
            "protocol-1",
            protocol.protocol_sha256,
            "eval-2",
            eval2.bundle_sha256,
            T3,
            predecessor_strategy_version_id="strategy-1",
            candidate_model_version_id="model-1",
        )
    )

    at_t3 = strategy_state_projection(registry, key, as_of=T3)
    assert [(item.strategy_version_id, item.state) for item in at_t3] == [
        ("strategy-1", StrategyLifecycleState.CHAMPION),
        ("strategy-2", StrategyLifecycleState.CHALLENGER),
    ]

    registry.record_promotion(
        PromotionDecision(
            "reject-2",
            PromotionAction.REJECT,
            "strategy-2",
            "protocol-1",
            protocol.protocol_sha256,
            "eval-2",
            eval2.bundle_sha256,
            T4,
            predecessor_strategy_version_id="strategy-1",
            candidate_model_version_id="model-1",
        )
    )

    reopened = ScientificRegistry(path)
    final = strategy_state_projection(reopened, key, as_of=T4)
    assert [(item.strategy_version_id, item.state) for item in final] == [
        ("strategy-1", StrategyLifecycleState.CHAMPION),
        ("strategy-2", StrategyLifecycleState.REJECTED),
    ]
    assert final[1].latest_decision_id == "reject-2"
    assert final[1].latest_decision_action is PromotionAction.REJECT


def test_strategy_lineage_projection_is_complete_and_causally_fenced(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    protocol, _, eval2 = _seed_two_strategies(registry)
    registry.record_promotion(
        PromotionDecision(
            "retain-2",
            PromotionAction.RETAIN,
            "strategy-2",
            "protocol-1",
            protocol.protocol_sha256,
            "eval-2",
            eval2.bundle_sha256,
            T3,
            predecessor_strategy_version_id="strategy-1",
            candidate_model_version_id="model-1",
        )
    )

    with pytest.raises(ScientificRegistryError, match="not causally available"):
        strategy_lineage_projection(registry, "strategy-2", as_of=T1)

    reopened = ScientificRegistry(path)
    lineage = strategy_lineage_projection(reopened, "strategy-2", as_of=T3)
    assert lineage.strategy.record_id == "strategy-2"
    assert [entry.record_id for entry in lineage.predecessor_strategies] == ["strategy-1"]
    assert [entry.record_id for entry in lineage.models] == ["model-1"]
    assert [entry.record_id for entry in lineage.datasets] == ["dataset-1"]
    assert [entry.record_id for entry in lineage.feature_sets] == ["features-1"]
    assert [entry.record_id for entry in lineage.protocols] == ["protocol-1"]
    assert [entry.record_id for entry in lineage.experiments] == [
        "experiment-1",
        "experiment-2",
    ]
    assert [entry.record_id for entry in lineage.evaluations] == ["eval-1", "eval-2"]
    assert [entry.record_id for entry in lineage.promotion_decisions] == ["retain-2"]
    assert lineage.postmortems == ()


def test_strategy_lineage_projection_traverses_model_predecessors(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    _seed_two_strategies(registry)
    registry.append(
        ModelVersion(
            "model-2",
            "fixture-model",
            SHA_B,
            SHA_C,
            SHA_D,
            "dataset-1",
            "features-1",
            "protocol-1",
            11,
            SHA_C,
            T3,
            predecessor_model_version_id="model-1",
        )
    )
    registry.append(
        StrategyVersion(
            "strategy-3",
            "predictive-edge:soccer:match-winner",
            SHA_C,
            SHA_D,
            SHA_C,
            T3,
            model_version_id="model-2",
            predecessor_strategy_version_id="strategy-2",
        )
    )

    lineage = strategy_lineage_projection(registry, "strategy-3", as_of=T3)
    assert [entry.record_id for entry in lineage.predecessor_strategies] == [
        "strategy-1",
        "strategy-2",
    ]
    assert [entry.record_id for entry in lineage.models] == ["model-1", "model-2"]
