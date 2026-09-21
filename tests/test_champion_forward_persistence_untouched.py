import hashlib
import json
from datetime import datetime

from autosport.scientific_registry import (
    DatasetSnapshot,
    EvaluationBundleRef,
    ExperimentRecord,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    PromotionAction,
    PromotionDecision,
    PromotionEvidence,
    PromotionEvidenceDirection,
    PromotionEvidenceValidity,
    ResearchOutcome,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
    StrategyVersion,
    promotion_holdout_access_id,
)
from autosport.strategy_experiment import ScientificProtocolBinding


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"
T5 = "2026-01-06T00:00:00+00:00"
T6 = "2026-01-07T00:00:00+00:00"
T7 = "2026-01-08T00:00:00+00:00"
T8 = "2026-01-09T00:00:00+00:00"


def _payload_sha(record) -> str:
    canonical = json.dumps(
        record.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _promotion_rule_text() -> str:
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
    causal_cutoff: str,
    frozen_at: str,
    feature_set_version: str = "v1",
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
        causal_cutoff=causal_cutoff,
        evaluation_design="sealed walk-forward holdout",
        feature_set_version=feature_set_version,
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split", "source split"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=_promotion_rule_text(),
        expected_artifacts=("evaluation bundle", "decision"),
        code_config_sha256=SHA_B,
        frozen_at_utc=frozen_at,
    )


def _promotion_evidence(
    *,
    experiment_id: str,
    protocol: ResearchProtocol,
    question: ResearchQuestion,
    hypothesis: Hypothesis,
    strategy_id: str,
    model_id: str,
    bundle: EvaluationBundleRef,
    dataset: DatasetSnapshot,
    created_at: str,
) -> PromotionEvidence:
    trial_family = f"{protocol.record_id}:confirmation-trial-family"
    payload = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "research_protocol_id": protocol.record_id,
        "research_question_id": question.question_id,
        "hypothesis_id": hypothesis.hypothesis_id,
        "candidate_strategy_version_id": strategy_id,
        "candidate_model_version_id": model_id,
        "evaluation_bundle_id": bundle.evaluation_bundle_id,
        "evaluation_bundle_sha256": bundle.bundle_sha256,
        "dataset_snapshot_id": dataset.dataset_snapshot_id,
        "holdout_access_id": promotion_holdout_access_id(
            research_protocol_id=protocol.record_id,
            dataset_manifest_sha256=dataset.manifest_sha256,
            source_identity=dataset.source_identity,
            license_identity=dataset.license_identity,
            confirmation_trial_family_id=trial_family,
        ),
        "confirmation_trial_family_id": trial_family,
        "estimand": hypothesis.primary_metric,
        "direction": PromotionEvidenceDirection.LOWER_IS_BETTER.value,
        "cohort_id": dataset.dataset_snapshot_id,
        "effective_sample_size": bundle.effective_sample_size,
        "minimum_effective_sample_size": 3,
        "effect_interval_low": bundle.effect_interval_low,
        "effect_interval_high": bundle.effect_interval_high,
        "practical_improvement": bundle.practical_improvement,
        "guardrails_passed": True,
        "validity": PromotionEvidenceValidity.ELIGIBLE.value,
        "holdout_consumed": False,
        "stopping_rule_sha256": hashlib.sha256(b"one final evaluation").hexdigest(),
        "multiple_comparison_control_sha256": hashlib.sha256(
            b"single frozen primary metric"
        ).hexdigest(),
        "rollback_identity": "NONE",
        "uncertainty_method": "bootstrap intervals",
        "created_at": created_at,
    }
    canonical_id = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    fields = {key: value for key, value in payload.items() if key != "schema_version"}
    fields["direction"] = PromotionEvidenceDirection(fields["direction"])
    fields["validity"] = PromotionEvidenceValidity(fields["validity"])
    return PromotionEvidence(canonical_id, **fields)


def test_champion_persistence_uses_genuinely_distinct_later_forward_evidence(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)

    champion_question = ResearchQuestion(
        "question-champion",
        "Does the champion clear the frozen primary metric?",
        SHA_A,
        T0,
    )
    champion_hypothesis = Hypothesis(
        "hypothesis-champion",
        champion_question.question_id,
        "Champion candidate improves the frozen primary metric.",
        "holdout ROI improves",
        "holdout ROI does not improve or a guardrail regresses",
        "roi",
        ("max_drawdown",),
        T0,
    )
    champion_protocol = ResearchProtocol(
        _binding(
            protocol_id="protocol-champion",
            question=champion_question,
            hypothesis=champion_hypothesis,
            causal_cutoff=T1,
            frozen_at=T0,
        ),
        SHA_C,
        SHA_D,
        SHA_A,
        T0,
    )
    champion_dataset = DatasetSnapshot(
        "dataset-champion",
        SHA_A,
        "lawful-provider:champion-fixture",
        "license-evidence:champion-v1",
        T1,
        T0,
        outcome_reveal_after=T1,
    )
    champion_features = FeatureSet("features-champion", "v1", SHA_B, SHA_C, T0)
    champion_model = ModelVersion(
        "model-champion",
        "fixture-model",
        SHA_A,
        SHA_C,
        SHA_D,
        champion_dataset.dataset_snapshot_id,
        champion_features.feature_set_id,
        champion_protocol.record_id,
        7,
        SHA_B,
        T1,
    )
    champion_strategy = StrategyVersion(
        "strategy-champion",
        "canonical-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T1,
        model_version_id=champion_model.model_version_id,
    )
    champion_bundle = EvaluationBundleRef(
        "eval-champion",
        SHA_D,
        SHA_C,
        champion_dataset.dataset_snapshot_id,
        champion_protocol.protocol_sha256,
        (SHA_A, SHA_B),
        T2,
        evaluated_strategy_version_id=champion_strategy.strategy_version_id,
        evaluated_model_version_id=champion_model.model_version_id,
        effective_sample_size=5,
        effect_interval_low="0.05",
        effect_interval_high="0.15",
        practical_improvement="0.1",
    )
    champion_experiment = ExperimentRecord(
        "experiment-champion",
        champion_protocol.record_id,
        champion_dataset.dataset_snapshot_id,
        champion_features.feature_set_id,
        champion_strategy.strategy_version_id,
        champion_bundle.evaluation_bundle_id,
        7,
        SHA_B,
        ResearchOutcome.POSITIVE,
        T1,
        model_version_id=champion_model.model_version_id,
        completed_at=T2,
        notes="frozen champion confirmation",
    )
    for record in (
        champion_question,
        champion_hypothesis,
        champion_protocol,
        champion_dataset,
        champion_features,
        champion_model,
        champion_strategy,
        champion_bundle,
        champion_experiment,
    ):
        registry.append(record)

    champion_evidence = _promotion_evidence(
        experiment_id=champion_experiment.experiment_id,
        protocol=champion_protocol,
        question=champion_question,
        hypothesis=champion_hypothesis,
        strategy_id=champion_strategy.strategy_version_id,
        model_id=champion_model.model_version_id,
        bundle=champion_bundle,
        dataset=champion_dataset,
        created_at=T3,
    )
    registry.append(champion_evidence)
    registry.record_promotion(
        PromotionDecision(
            "promote-champion",
            PromotionAction.PROMOTE,
            champion_strategy.strategy_version_id,
            champion_protocol.record_id,
            champion_protocol.protocol_sha256,
            champion_bundle.evaluation_bundle_id,
            champion_bundle.bundle_sha256,
            T3,
            candidate_model_version_id=champion_model.model_version_id,
            promotion_evidence_id=champion_evidence.promotion_evidence_id,
        )
    )
    assert registry.champion_strategy(
        as_of=T3,
        canonical_strategy_id="canonical-strategy",
    ) == champion_strategy.strategy_version_id

    forward_question = ResearchQuestion(
        "question-forward",
        "Does a later challenger beat the durable champion?",
        SHA_E,
        T4,
    )
    forward_hypothesis = Hypothesis(
        "hypothesis-forward",
        forward_question.question_id,
        "The later challenger improves the frozen forward metric.",
        "untouched forward ROI improves",
        "untouched forward ROI does not improve or a guardrail regresses",
        "roi",
        ("max_drawdown",),
        T4,
    )
    forward_protocol = ResearchProtocol(
        _binding(
            protocol_id="protocol-forward",
            question=forward_question,
            hypothesis=forward_hypothesis,
            causal_cutoff=T5,
            frozen_at=T4,
        ),
        SHA_E,
        SHA_D,
        SHA_E,
        T4,
    )
    forward_dataset = DatasetSnapshot(
        "dataset-forward",
        SHA_E,
        "lawful-provider:forward-fixture",
        "license-evidence:forward-v1",
        T5,
        T4,
        outcome_reveal_after=T5,
    )
    forward_features = FeatureSet("features-forward", "v1", SHA_F, SHA_C, T4)
    forward_model = ModelVersion(
        "model-forward",
        "fixture-model",
        SHA_E,
        SHA_C,
        SHA_D,
        forward_dataset.dataset_snapshot_id,
        forward_features.feature_set_id,
        forward_protocol.record_id,
        11,
        SHA_B,
        T5,
        predecessor_model_version_id=champion_model.model_version_id,
    )
    forward_strategy = StrategyVersion(
        "strategy-forward",
        "canonical-strategy",
        SHA_F,
        SHA_D,
        SHA_B,
        T5,
        model_version_id=forward_model.model_version_id,
        predecessor_strategy_version_id=champion_strategy.strategy_version_id,
    )
    forward_bundle = EvaluationBundleRef(
        "eval-forward",
        SHA_F,
        SHA_C,
        forward_dataset.dataset_snapshot_id,
        forward_protocol.protocol_sha256,
        (SHA_E, SHA_F),
        T6,
        evaluated_strategy_version_id=forward_strategy.strategy_version_id,
        evaluated_model_version_id=forward_model.model_version_id,
        effective_sample_size=5,
        effect_interval_low="-0.15",
        effect_interval_high="-0.05",
        practical_improvement="-0.1",
    )
    forward_experiment = ExperimentRecord(
        "experiment-forward",
        forward_protocol.record_id,
        forward_dataset.dataset_snapshot_id,
        forward_features.feature_set_id,
        forward_strategy.strategy_version_id,
        forward_bundle.evaluation_bundle_id,
        11,
        SHA_B,
        ResearchOutcome.NEGATIVE,
        T5,
        model_version_id=forward_model.model_version_id,
        completed_at=T6,
        notes="negative result on distinct later forward evidence",
    )
    for record in (
        forward_question,
        forward_hypothesis,
        forward_protocol,
        forward_dataset,
        forward_features,
        forward_model,
        forward_strategy,
        forward_bundle,
        forward_experiment,
    ):
        registry.append(record)

    assert forward_protocol.record_id != champion_protocol.record_id
    assert forward_dataset.dataset_snapshot_id != champion_dataset.dataset_snapshot_id
    assert forward_dataset.manifest_sha256 != champion_dataset.manifest_sha256
    assert datetime.fromisoformat(
        forward_protocol.binding.frozen_at_utc
    ) < datetime.fromisoformat(forward_dataset.outcome_reveal_after)
    assert datetime.fromisoformat(
        forward_dataset.causal_cutoff
    ) > datetime.fromisoformat(champion_dataset.causal_cutoff)
    assert registry.causal_precedes(
        "ResearchProtocol",
        forward_protocol.record_id,
        "EvaluationBundle",
        forward_bundle.evaluation_bundle_id,
    )
    assert registry.causal_precedes(
        "DatasetSnapshot",
        forward_dataset.dataset_snapshot_id,
        "EvaluationBundle",
        forward_bundle.evaluation_bundle_id,
    )
    assert registry.champion_strategy(
        as_of=T6,
        canonical_strategy_id="canonical-strategy",
    ) == champion_strategy.strategy_version_id

    registry.record_promotion(
        PromotionDecision(
            "retain-forward-challenger",
            PromotionAction.RETAIN,
            forward_strategy.strategy_version_id,
            forward_protocol.record_id,
            forward_protocol.protocol_sha256,
            forward_bundle.evaluation_bundle_id,
            forward_bundle.bundle_sha256,
            T7,
            predecessor_strategy_version_id=champion_strategy.strategy_version_id,
            candidate_model_version_id=forward_model.model_version_id,
            reason="distinct later forward evidence did not replace the champion",
        )
    )

    reopened = ScientificRegistry(path)
    assert reopened.champion_strategy(
        as_of=T7,
        canonical_strategy_id="canonical-strategy",
    ) == champion_strategy.strategy_version_id
    assert reopened.champion_strategy(
        as_of=T8,
        canonical_strategy_id="canonical-strategy",
    ) == champion_strategy.strategy_version_id
