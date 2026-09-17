import hashlib
import json
from dataclasses import replace

import pytest

from autosport.scientific_registry import (
    DatasetSnapshot,
    DuplicateExperimentFingerprintError,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    ResearchOutcome,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
    StrategyVersion,
)
from autosport.strategy_experiment import ScientificProtocolBinding
from autosport.strategy_model_factory import DriftEvidence, PromotionRule, PromotionVerdict, TrainingPoint
from autosport.strategy_model_factory_registry import (
    FactoryRegistryRunSpec,
    ScientificFactoryRegistryBridge,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
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


def _foundation(registry: ScientificRegistry) -> None:
    question = ResearchQuestion(
        "factory-question",
        "Does the frozen mean baseline challenger improve causal walk-forward MSE?",
        SHA_A,
        T0,
    )
    hypothesis = Hypothesis(
        "factory-hypothesis",
        question.question_id,
        "The challenger lowers walk-forward MSE without degrading protective metrics.",
        "challenger MSE improves by at least the frozen threshold",
        "reject on insufficient MSE improvement or any protective-metric breach",
        "mse",
        ("max_drawdown",),
        T0,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="factory-protocol",
        research_question_id=question.question_id,
        research_question_sha256=_payload_sha(question),
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=_payload_sha(hypothesis),
        inclusion_criteria="predeclared timestamped observations",
        exclusion_criteria="missing provenance or duplicate observation time",
        lawful_source_requirements="lawful retained source evidence",
        causal_cutoff=T1,
        evaluation_design="expanding-window walk-forward",
        feature_set_version="v1",
        uncertainty_method="deterministic baseline proof",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time ordering", "restart readback"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one frozen final factory evaluation",
        promotion_rule="lower MSE by threshold and pass all protective maxima",
        expected_artifacts=("walk-forward evaluation", "promotion decision", "reproducibility bundle"),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, SHA_A, T0)
    dataset = DatasetSnapshot(
        "factory-dataset",
        SHA_A,
        "lawful-provider:factory-fixture",
        "license-evidence:v1",
        T1,
        T0,
        outcome_reveal_after=T1,
    )
    features = FeatureSet("factory-features", "v1", SHA_B, SHA_C, T0)
    champion = StrategyVersion(
        "factory-champion",
        "factory-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T1,
    )
    for record in (question, hypothesis, protocol, dataset, features, champion):
        registry.append(record)


def _spec() -> FactoryRegistryRunSpec:
    model = ModelVersion(
        "factory-model-challenger",
        "mean-baseline-v1",
        SHA_E,
        SHA_C,
        SHA_D,
        "factory-dataset",
        "factory-features",
        "factory-protocol",
        7,
        SHA_B,
        T1,
    )
    strategy = StrategyVersion(
        "factory-strategy-challenger",
        "factory-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T1,
        model_version_id=model.model_version_id,
        predecessor_strategy_version_id="factory-champion",
    )
    return FactoryRegistryRunSpec(
        model=model,
        strategy=strategy,
        evaluation_bundle_id="factory-evaluation",
        experiment_id="factory-experiment",
        promotion_decision_id="factory-decision",
        started_at=T2,
        completed_at=T3,
        decided_at=T4,
        evaluator_source_sha256=SHA_C,
        rejected_outcome=ResearchOutcome.NEGATIVE,
        postmortem_id="factory-postmortem",
        retest_conditions=("new frozen protocol", "new lawful dataset snapshot"),
    )


def _points():
    return (
        TrainingPoint("2026-01-01T00:00:00+00:00", 1.0, 0.0),
        TrainingPoint("2026-01-02T00:00:00+00:00", 2.0, 0.0),
        TrainingPoint("2026-01-03T00:00:00+00:00", 3.0, 1.0),
        TrainingPoint("2026-01-04T00:00:00+00:00", 4.0, 1.0),
    )


def test_registry_backed_factory_rejection_survives_restart_and_blocks_blind_rerun(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    _foundation(registry)
    bridge = ScientificFactoryRegistryBridge(registry)
    spec = _spec()

    result = bridge.run(
        spec,
        _points(),
        promotion_rule=PromotionRule("mse", 0.05, (("max_drawdown", 0.20),)),
        champion_metrics={"mse": 0.10, "max_drawdown": 0.10},
        challenger_protective_metrics={"max_drawdown": 0.10},
        drift_evidence=(
            DriftEvidence("calibration", 0.02, 0.20, 0.05, T3),
        ),
        minimum_train_size=2,
    )

    assert result.promotion.verdict is PromotionVerdict.REJECT
    assert result.drift_recommendations == (f"RESEARCH_CHALLENGER:calibration:{T3}",)
    assert len(result.evaluation_artifact_sha256) == 64
    assert len(result.reproducibility_bundle_sha256) == 64

    reopened = ScientificRegistry(path)
    experiment = reopened.get("Experiment", spec.experiment_id)
    decision = reopened.get("PromotionDecision", spec.promotion_decision_id)
    assert experiment is not None
    assert experiment.payload["outcome"] == ResearchOutcome.NEGATIVE.value
    assert decision is not None
    assert decision.payload["action"] == "REJECT"
    assert reopened.get("Postmortem", "factory-postmortem") is not None
    assert reopened.find_experiment_fingerprint(result.experiment_fingerprint)[0].record_id == spec.experiment_id
    assert reopened.reproducibility_bundle(spec.experiment_id)["bundle_sha256"] == result.reproducibility_bundle_sha256

    durable_notes = json.loads(experiment.payload["notes"])
    durable_artifact = durable_notes["factory_evaluation_artifact"]
    assert durable_artifact["drift_evidence"][0]["drifted"] is True
    assert durable_artifact["drift_recommendations"] == [
        f"RESEARCH_CHALLENGER:calibration:{T3}"
    ]
    assert durable_artifact["truth"] == {
        "automatic_financial_authority_expansion": False,
        "real_money_execution": False,
    }

    duplicate = replace(
        spec,
        experiment_id="factory-experiment-repeat",
        promotion_decision_id="factory-decision-repeat",
        postmortem_id="factory-postmortem-repeat",
    )
    with pytest.raises(DuplicateExperimentFingerprintError, match="durable history"):
        ScientificFactoryRegistryBridge(reopened).run(
            duplicate,
            tuple(reversed(_points())),
            promotion_rule=PromotionRule("mse", 0.05, (("max_drawdown", 0.20),)),
            champion_metrics={"mse": 0.10, "max_drawdown": 0.10},
            challenger_protective_metrics={"max_drawdown": 0.10},
            drift_evidence=(DriftEvidence("calibration", 0.02, 0.20, 0.05, T3),),
            minimum_train_size=2,
        )


def test_factory_fails_before_evaluation_when_frozen_registry_contract_mismatches(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    _foundation(registry)
    spec = _spec()
    bad_model = replace(spec.model, config_sha256=SHA_E)
    bad_strategy = replace(spec.strategy, model_version_id=bad_model.model_version_id, config_sha256=SHA_E)
    bad_spec = replace(spec, model=bad_model, strategy=bad_strategy)

    with pytest.raises(ValueError, match="frozen research protocol"):
        ScientificFactoryRegistryBridge(registry).run(
            bad_spec,
            _points(),
            promotion_rule=PromotionRule("mse", 0.05),
            champion_metrics={"mse": 0.10},
        )

    assert registry.get("ModelVersion", bad_model.model_version_id) is None
    assert registry.get("Experiment", bad_spec.experiment_id) is None
