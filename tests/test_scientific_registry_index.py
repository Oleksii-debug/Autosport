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
from autosport.scientific_registry_index import ScientificRegistryIndex, StrategyLifecycleState
from autosport.strategy_experiment import ScientificProtocolBinding


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"


def _seed_registry(path):
    registry = ScientificRegistry.initialize_pristine(path)
    question = ResearchQuestion("question-1", "Frozen question", SHA_A, T0)
    hypothesis = Hypothesis(
        "hypothesis-1",
        "question-1",
        "Candidate improves the primary metric.",
        "primary > champion",
        "primary <= champion",
        "primary",
        ("drawdown",),
        T0,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="protocol-1",
        research_question_id="question-1",
        research_question_sha256=_payload_sha(question),
        hypothesis_id="hypothesis-1",
        hypothesis_sha256=_payload_sha(hypothesis),
        inclusion_criteria="frozen",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="lawful fixture",
        causal_cutoff=T1,
        evaluation_design="walk-forward holdout",
        feature_set_version="v1",
        uncertainty_method="bootstrap",
        multiple_comparison_control="single primary",
        robustness_checks=("time split",),
        random_seed_policy="fixed",
        stopping_rule="one final evaluation",
        promotion_rule="primary improves and guardrails pass",
        expected_artifacts=("evaluation bundle",),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, SHA_A, T0)
    dataset = DatasetSnapshot("dataset-1", SHA_A, "fixture", "fixture-rights", T1, T0)
    features = FeatureSet("features-1", "v1", SHA_B, SHA_C, T0)
    model = ModelVersion(
        "model-1",
        "baseline",
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
        "paper-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T1,
        model_version_id="model-1",
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
    experiment1 = ExperimentRecord(
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
    for record in (question, hypothesis, protocol, dataset, features, model, strategy1, eval1, experiment1):
        registry.append(record)
    registry.record_promotion(
        PromotionDecision(
            "promotion-1",
            PromotionAction.PROMOTE,
            "strategy-1",
            "protocol-1",
            protocol.protocol_sha256,
            "eval-1",
            eval1.bundle_sha256,
            T2,
            candidate_model_version_id="model-1",
        )
    )

    strategy2 = StrategyVersion(
        "strategy-2",
        "paper-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T2,
        model_version_id="model-1",
        predecessor_strategy_version_id="strategy-1",
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
    experiment2 = ExperimentRecord(
        "experiment-2",
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-2",
        "eval-2",
        7,
        SHA_B,
        ResearchOutcome.NULL,
        T2,
        model_version_id="model-1",
        completed_at=T2,
    )
    for record in (strategy2, eval2, experiment2):
        registry.append(record)
    registry.record_promotion(
        PromotionDecision(
            "rejection-2",
            PromotionAction.REJECT,
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
    return registry


def _payload_sha(record) -> str:
    import hashlib
    import json

    payload = json.dumps(record.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_strategy_state_is_causal_and_restart_deterministic(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = _seed_registry(path)
    index = ScientificRegistryIndex(registry)

    before_rejection = index.strategy_state("paper-strategy", as_of=T2)
    assert before_rejection.champion_strategy_version_id == "strategy-1"
    assert before_rejection.state_of("strategy-1") is StrategyLifecycleState.PROMOTED
    assert before_rejection.state_of("strategy-2") is StrategyLifecycleState.CHALLENGER

    after_rejection = index.strategy_state("paper-strategy", as_of=T3)
    assert after_rejection.champion_strategy_version_id == "strategy-1"
    assert after_rejection.state_of("strategy-2") is StrategyLifecycleState.REJECTED

    reopened = ScientificRegistryIndex(ScientificRegistry(path))
    assert reopened.strategy_state("paper-strategy", as_of=T3) == after_rejection


def test_lineage_indexes_bind_dataset_model_and_strategy_without_future_decisions(tmp_path):
    registry = _seed_registry(tmp_path / "scientific_registry.json")
    index = ScientificRegistryIndex(registry)

    dataset = index.by_dataset("dataset-1", as_of=T3)
    assert [entry.record_id for entry in dataset.models] == ["model-1"]
    assert [entry.record_id for entry in dataset.strategies] == ["strategy-1", "strategy-2"]
    assert [entry.record_id for entry in dataset.experiments] == ["experiment-1", "experiment-2"]
    assert [entry.record_id for entry in dataset.evaluations] == ["eval-1", "eval-2"]
    assert [entry.record_id for entry in dataset.promotions] == ["promotion-1", "rejection-2"]

    model = index.by_model("model-1", as_of=T3)
    assert [entry.record_id for entry in model.strategies] == ["strategy-1", "strategy-2"]
    assert [entry.record_id for entry in model.datasets] == ["dataset-1"]

    strategy = index.by_strategy("strategy-2", as_of=T2)
    assert [entry.record_id for entry in strategy.strategies] == ["strategy-1", "strategy-2"]
    assert [entry.record_id for entry in strategy.models] == ["model-1"]
    assert [entry.record_id for entry in strategy.experiments] == ["experiment-1", "experiment-2"]
    assert [entry.record_id for entry in strategy.evaluations] == ["eval-1", "eval-2"]
    assert [entry.record_id for entry in strategy.promotions] == ["promotion-1"]


def test_lineage_indexes_traverse_strategy_and_model_predecessors(tmp_path):
    registry = _seed_registry(tmp_path / "scientific_registry.json")
    registry.append(
        ModelVersion(
            "model-2",
            "baseline",
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
            "paper-strategy",
            SHA_C,
            SHA_D,
            SHA_C,
            T3,
            model_version_id="model-2",
            predecessor_strategy_version_id="strategy-2",
        )
    )

    index = ScientificRegistryIndex(registry)
    model = index.by_model("model-2", as_of=T3)
    assert [entry.record_id for entry in model.models] == ["model-1", "model-2"]

    strategy = index.by_strategy("strategy-3", as_of=T3)
    assert [entry.record_id for entry in strategy.strategies] == [
        "strategy-1",
        "strategy-2",
        "strategy-3",
    ]
    assert [entry.record_id for entry in strategy.models] == ["model-1", "model-2"]


def test_independent_strategy_contexts_have_independent_champions(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = _seed_registry(path)
    protocol = registry.get("ResearchProtocol", "protocol-1")
    assert protocol is not None
    protocol_sha256 = protocol.payload["protocol_sha256"]

    context_b_strategy = StrategyVersion(
        "strategy-context-b-1",
        "totals-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T2,
        model_version_id="model-1",
    )
    context_b_eval = EvaluationBundleRef(
        "eval-context-b-1",
        SHA_C,
        SHA_C,
        "dataset-1",
        protocol_sha256,
        (SHA_D,),
        T2,
        evaluated_strategy_version_id="strategy-context-b-1",
        evaluated_model_version_id="model-1",
    )
    context_b_experiment = ExperimentRecord(
        "experiment-context-b-1",
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-context-b-1",
        "eval-context-b-1",
        7,
        SHA_B,
        ResearchOutcome.POSITIVE,
        T2,
        model_version_id="model-1",
        completed_at=T2,
    )
    for record in (context_b_strategy, context_b_eval, context_b_experiment):
        registry.append(record)

    before_promotion = ScientificRegistryIndex(registry).strategy_state("totals-strategy", as_of=T2)
    assert before_promotion.champion_strategy_version_id is None
    assert before_promotion.state_of("strategy-context-b-1") is StrategyLifecycleState.CHALLENGER

    registry.record_promotion(
        PromotionDecision(
            "z-promotion-context-b-1",
            PromotionAction.PROMOTE,
            "strategy-context-b-1",
            "protocol-1",
            protocol_sha256,
            "eval-context-b-1",
            context_b_eval.bundle_sha256,
            T3,
            candidate_model_version_id="model-1",
        )
    )

    index = ScientificRegistryIndex(registry)
    assert index.strategy_state("paper-strategy", as_of=T3).champion_strategy_version_id == "strategy-1"
    context_b = index.strategy_state("totals-strategy", as_of=T3)
    assert context_b.champion_strategy_version_id == "strategy-context-b-1"
    assert context_b.state_of("strategy-context-b-1") is StrategyLifecycleState.PROMOTED

    reopened = ScientificRegistryIndex(ScientificRegistry(path))
    assert reopened.strategy_state("totals-strategy", as_of=T3) == context_b
