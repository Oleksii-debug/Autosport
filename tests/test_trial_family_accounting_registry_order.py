from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from autosport.research_multiplicity import (
    ExperimentFamilyMember,
    ExperimentFamilyPlan,
    MetricDirection,
    MultiplicityControlKind,
)
from autosport.scientific_registry import (
    DatasetSnapshot,
    EvaluationBundleRef,
    ExperimentRecord,
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
from autosport.trial_family_accounting import (
    TrialAttemptStatus,
    TrialCandidateLineage,
    TrialFamilyAccountingStore,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-09-20T00:00:00Z"
T1 = "2026-09-20T01:00:00Z"
T2 = "2026-09-20T02:00:00Z"
T3 = "2026-09-20T03:00:00Z"


def _payload_sha(record) -> str:
    canonical = json.dumps(
        record.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _foundation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    TrialFamilyAccountingStore.initialize_workspace(workspace)
    registry = ScientificRegistry.initialize_pristine(workspace / "scientific.json")

    question = ResearchQuestion(
        "question-1",
        "Does the frozen candidate improve the primary metric?",
        SHA_A,
        T0,
    )
    hypothesis = Hypothesis(
        "hypothesis-1",
        "question-1",
        "Candidate improves frozen ROI.",
        "holdout ROI improves",
        "ROI fails to improve or a guardrail regresses",
        "roi",
        ("max_drawdown",),
        T0,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="protocol-1",
        research_question_id=question.question_id,
        research_question_sha256=_payload_sha(question),
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=_payload_sha(hypothesis),
        inclusion_criteria="predeclared lawful events",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="retained lawful source evidence",
        causal_cutoff=T1,
        evaluation_design="sealed walk-forward holdout",
        feature_set_version="v1",
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="one frozen alpha-spending family",
        robustness_checks=("time split", "source split"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="at most two predeclared looks",
        promotion_rule=json.dumps(
            {
                "kind": "autosport-promotion-rule-v1",
                "primary_metric": "roi",
                "minimum_improvement": 0.01,
                "minimum_effective_sample_size": 3,
                "protective_metric_maxima": [],
                "metric_direction": "higher_is_better",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
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
    strategy = StrategyVersion(
        "strategy-1",
        "canonical-strategy",
        SHA_C,
        SHA_D,
        SHA_B,
        T1,
        model_version_id="model-1",
    )
    for record in (question, hypothesis, protocol, dataset, features, model, strategy):
        registry.append(record)

    candidate = TrialCandidateLineage(
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        dataset_snapshot_id="dataset-1",
        feature_set_id="features-1",
        seed=7,
        config_sha256=SHA_B,
    )
    member = ExperimentFamilyMember(
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=_payload_sha(hypothesis),
        semantic_variant_sha256=candidate.semantic_sha256,
        candidate_label="candidate display label",
    )
    plan = ExperimentFamilyPlan(
        family_id="display-family-name",
        research_protocol_id=protocol.record_id,
        protocol_sha256=protocol.protocol_sha256,
        research_question_id=question.question_id,
        primary_metric="roi",
        direction=MetricDirection.HIGHER_IS_BETTER,
        control_kind=MultiplicityControlKind.FWER,
        method_id=ExperimentFamilyPlan.IMPLEMENTED_METHOD,
        stopping_rule=binding.stopping_rule,
        familywise_alpha=Decimal("0.05"),
        look_alpha_spend=(Decimal("0.01"), Decimal("0.01")),
        members=(member,),
        frozen_at=T0,
    )
    store = TrialFamilyAccountingStore.initialize_pristine(
        workspace / "trial-family.json",
        registry,
        plan,
        workspace_root=workspace,
        authority_root=tmp_path / "machine-authority",
    )
    return registry, protocol, candidate, member, store


def _bundle(protocol: ResearchProtocol) -> EvaluationBundleRef:
    return EvaluationBundleRef(
        "eval-late",
        SHA_D,
        SHA_C,
        "dataset-1",
        protocol.protocol_sha256,
        (SHA_A, SHA_B),
        T2,
        evaluated_strategy_version_id="strategy-1",
        evaluated_model_version_id="model-1",
    )


def _experiment() -> ExperimentRecord:
    return ExperimentRecord(
        "experiment-late-bundle",
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-1",
        "eval-late",
        7,
        SHA_B,
        ResearchOutcome.NEGATIVE,
        T1,
        model_version_id="model-1",
        completed_at=T3,
        notes="result published before its referenced evaluation bundle",
    )


def test_completion_rejects_bundle_appended_after_experiment_even_if_backdated(tmp_path):
    registry, protocol, candidate, member, store = _foundation(tmp_path)
    attempt = store.start_attempt(
        semantic_attempt_id="late-evaluation-evidence",
        member_authority_id=member.member_authority_id,
        candidate=candidate,
        created_at=T1,
    )
    experiment = _experiment()
    registry.append(experiment)
    registry.append(_bundle(protocol))

    with pytest.raises(ValueError, match="durably published before the Experiment"):
        store.complete_attempt(
            attempt_id=attempt.attempt_id,
            experiment_id=experiment.experiment_id,
            registry=registry,
        )
    assert store.attempts()[0].status is TrialAttemptStatus.OPEN


def test_completion_accepts_bundle_published_before_experiment(tmp_path):
    registry, protocol, candidate, member, store = _foundation(tmp_path)
    attempt = store.start_attempt(
        semantic_attempt_id="ordered-evaluation-evidence",
        member_authority_id=member.member_authority_id,
        candidate=candidate,
        created_at=T1,
    )
    bundle = _bundle(protocol)
    experiment = _experiment()
    registry.append(bundle)
    registry.append(experiment)

    completed = store.complete_attempt(
        attempt_id=attempt.attempt_id,
        experiment_id=experiment.experiment_id,
        registry=registry,
    )
    assert completed.status is TrialAttemptStatus.COMPLETED
    assert completed.experiment_id == experiment.experiment_id
