import hashlib
import json

import pytest

from autosport.scientific_registry import (
    DatasetSnapshot,
    DuplicateExperimentFingerprintError,
    EvaluationBundleRef,
    ExperimentRecord,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    Postmortem,
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
T4 = "2026-01-05T00:00:00+00:00"
T5 = "2026-01-06T00:00:00+00:00"


def _payload_sha(record) -> str:
    canonical = json.dumps(
        record.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _promotion_rule() -> str:
    return json.dumps(
        {
            "kind": "autosport-promotion-rule-v1",
            "primary_metric": "roi",
            "minimum_improvement": 0.05,
            "minimum_effective_sample_size": 3,
            "protective_metric_maxima": [],
            "metric_direction": "lower_is_better",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _binding(
    *,
    protocol_id: str,
    question: ResearchQuestion,
    hypothesis: Hypothesis,
) -> ScientificProtocolBinding:
    return ScientificProtocolBinding(
        research_protocol_id=protocol_id,
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
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split", "source split"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=_promotion_rule(),
        expected_artifacts=("evaluation bundle", "decision"),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )


def _install_first_negative(registry: ScientificRegistry) -> dict[str, object]:
    question = ResearchQuestion(
        "question-1",
        "Does candidate improve holdout ROI?",
        SHA_A,
        T0,
    )
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
    protocol = ResearchProtocol(
        _binding(protocol_id="protocol-1", question=question, hypothesis=hypothesis),
        SHA_C,
        SHA_D,
        SHA_A,
        T0,
    )
    dataset = DatasetSnapshot(
        "dataset-1",
        SHA_A,
        "lawful-provider:fixture",
        "license-evidence:v1",
        T1,
        T0,
        outcome_reveal_after=T1,
    )
    feature = FeatureSet("features-1", "v1", SHA_B, SHA_C, T0)
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
    for record in (question, hypothesis, protocol, dataset, feature, model, strategy, bundle):
        registry.append(record)

    experiment = ExperimentRecord(
        "experiment-1",
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-1",
        "eval-1",
        7,
        SHA_B,
        ResearchOutcome.NEGATIVE,
        T1,
        model_version_id="model-1",
        completed_at=T2,
        notes="frozen negative result",
    )
    registry.append(experiment)
    registry.append(
        Postmortem(
            "postmortem-1",
            "experiment-1",
            ResearchOutcome.NEGATIVE,
            "No primary-metric improvement.",
            ("material scientific change required before retest",),
            T3,
        )
    )
    return {
        "question": question,
        "hypothesis": hypothesis,
        "protocol": protocol,
        "experiment": experiment,
    }


def _append_protocol_alias_scaffolding(
    registry: ScientificRegistry,
    *,
    question: ResearchQuestion,
    hypothesis: Hypothesis,
) -> ExperimentRecord:
    protocol = ResearchProtocol(
        _binding(protocol_id="protocol-2", question=question, hypothesis=hypothesis),
        SHA_C,
        SHA_D,
        SHA_A,
        T0,
    )
    model = ModelVersion(
        "model-2",
        "fixture-model",
        SHA_A,
        SHA_C,
        SHA_D,
        "dataset-1",
        "features-1",
        "protocol-2",
        7,
        SHA_B,
        T4,
    )
    strategy = StrategyVersion(
        "strategy-2",
        "canonical-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T4,
        model_version_id="model-2",
    )
    bundle = EvaluationBundleRef(
        "eval-2",
        SHA_D,
        SHA_C,
        "dataset-1",
        protocol.protocol_sha256,
        (SHA_A, SHA_B),
        T4,
        evaluated_strategy_version_id="strategy-2",
        evaluated_model_version_id="model-2",
        effective_sample_size=5,
        effect_interval_low="0.05",
        effect_interval_high="0.15",
        practical_improvement="0.1",
    )
    for record in (protocol, model, strategy, bundle):
        registry.append(record)

    return ExperimentRecord(
        "experiment-2",
        "protocol-2",
        "dataset-1",
        "features-1",
        "strategy-2",
        "eval-2",
        7,
        SHA_B,
        ResearchOutcome.NEGATIVE,
        T4,
        model_version_id="model-2",
        completed_at=T5,
        notes="same scientific hypothesis repackaged under alias identities",
    )


def test_negative_memory_cannot_be_laundered_through_protocol_alias(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    foundation = _install_first_negative(registry)
    alias = _append_protocol_alias_scaffolding(
        registry,
        question=foundation["question"],
        hypothesis=foundation["hypothesis"],
    )

    original = foundation["experiment"]
    assert original.fingerprint != alias.fingerprint

    protocol_1 = registry.get("ResearchProtocol", "protocol-1")
    protocol_2 = registry.get("ResearchProtocol", "protocol-2")
    assert protocol_1 is not None
    assert protocol_2 is not None
    assert (
        protocol_1.payload["binding"]["hypothesis_sha256"]
        == protocol_2.payload["binding"]["hypothesis_sha256"]
    )
    assert protocol_1.payload["binding"]["research_question_sha256"] == (
        protocol_2.payload["binding"]["research_question_sha256"]
    )

    # The prior NEGATIVE + Postmortem is canonical scientific memory. Merely
    # reissuing the same frozen hypothesis/protocol semantics under new
    # protocol/model/strategy/evaluation IDs must not turn it into fresh work.
    with pytest.raises(DuplicateExperimentFingerprintError):
        registry.append(alias)

    reopened = ScientificRegistry(path)
    assert reopened.get("Experiment", "experiment-1") is not None
    assert reopened.get("Postmortem", "postmortem-1") is not None
    assert reopened.get("Experiment", "experiment-2") is None
