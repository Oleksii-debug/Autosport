import hashlib
import json
from dataclasses import replace

import pytest

from autosport.scientific_registry import (
    AblationAuthorityEvidence,
    AblationAuthorityKind,
    ConflictingScientificRecordError,
    DatasetSnapshot,
    DuplicateExperimentFingerprintError,
    EvaluationBundleRef,
    ExperimentRecord,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    Postmortem,
    PromotionAction,
    PromotionDecision,
    PromotionEvidence,
    PromotionEvidenceDirection,
    PromotionEvidenceValidity,
    PromotionEvidenceError,
    ResearchOutcome,
    ResearchProtocol,
    ResearchQuestion,
    RetestCondition,
    ScientificEvidenceRef,
    ScientificRegistry,
    promotion_holdout_access_id,
    StrategyVersion,
)
from autosport.strategy_experiment import ScientificProtocolBinding
from autosport.workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockBusyError


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"


def _question() -> ResearchQuestion:
    return ResearchQuestion("question-1", "Does candidate improve holdout ROI?", SHA_A, T0)


def _hypothesis() -> Hypothesis:
    return Hypothesis(
        "hypothesis-1",
        "question-1",
        "Candidate improves the frozen primary metric.",
        "holdout ROI > champion ROI",
        "holdout ROI <= champion ROI or any guardrail regresses",
        "roi",
        ("max_drawdown",),
        T0,
    )


def _payload_sha(record) -> str:
    canonical = json.dumps(
        record.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _binding(
    question: ResearchQuestion | None = None,
    hypothesis: Hypothesis | None = None,
    promotion_rule: str | None = None,
) -> ScientificProtocolBinding:
    question = question or _question()
    hypothesis = hypothesis or _hypothesis()
    return ScientificProtocolBinding(
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
        promotion_rule=promotion_rule or _frozen_promotion_rule_text(),
        expected_artifacts=("evaluation bundle", "decision"),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )


def _frozen_promotion_rule_text(
    *, minimum_improvement: float = 0.05, minimum_effective_sample_size: int = 3
) -> str:
    return json.dumps(
        {
            "kind": "autosport-promotion-rule-v1",
            "primary_metric": "roi",
            "minimum_improvement": minimum_improvement,
            "minimum_effective_sample_size": minimum_effective_sample_size,
            "protective_metric_maxima": [],
            "metric_direction": "lower_is_better",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _foundation_with_binding(
    registry: ScientificRegistry,
    *,
    promotion_rule: str,
    effective_sample_size: int = 5,
    effect_interval_low: str = "0.05",
    effect_interval_high: str = "0.15",
    practical_improvement: str = "0.1",
) -> dict[str, object]:
    question = _question()
    hypothesis = _hypothesis()
    protocol = ResearchProtocol(_binding(question, hypothesis, promotion_rule), SHA_C, SHA_D, SHA_A, T0)
    dataset = DatasetSnapshot(
        "dataset-1", SHA_A, "lawful-provider:fixture", "license-evidence:v1",
        T1, T0, outcome_reveal_after=T1,
    )
    features = FeatureSet("features-1", "v1", SHA_B, SHA_C, T0)
    model = ModelVersion("model-1", "fixture-model", SHA_A, SHA_C, SHA_D,
                         "dataset-1", "features-1", "protocol-1", 7, SHA_B, T1)
    strategy = StrategyVersion("strategy-1", "canonical-strategy", SHA_C, SHA_D, SHA_B,
                               T1, model_version_id="model-1")
    bundle = EvaluationBundleRef("eval-1", SHA_D, SHA_C, "dataset-1",
                                 protocol.protocol_sha256, (SHA_A, SHA_B), T2,
                                 evaluated_strategy_version_id="strategy-1",
                                 evaluated_model_version_id="model-1",
                                 effective_sample_size=effective_sample_size,
                                 effect_interval_low=effect_interval_low,
                                 effect_interval_high=effect_interval_high,
                                 practical_improvement=practical_improvement)
    for record in (question, hypothesis, protocol, dataset, features, model, strategy, bundle):
        registry.append(record)
    return {"question": question, "hypothesis": hypothesis, "protocol": protocol,
            "dataset": dataset, "features": features, "model": model,
            "strategy": strategy, "bundle": bundle}


def _foundation(registry: ScientificRegistry) -> dict[str, object]:
    question = _question()
    hypothesis = _hypothesis()
    protocol = ResearchProtocol(_binding(question, hypothesis), SHA_C, SHA_D, SHA_A, T0)
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
    for record in (question, hypothesis, protocol, dataset, features, model, strategy, bundle):
        registry.append(record)
    return {
        "question": question,
        "hypothesis": hypothesis,
        "protocol": protocol,
        "dataset": dataset,
        "features": features,
        "model": model,
        "strategy": strategy,
        "bundle": bundle,
    }


def _experiment(*, experiment_id: str = "experiment-1", outcome=ResearchOutcome.NEGATIVE):
    return ExperimentRecord(
        experiment_id,
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-1",
        "eval-1",
        7,
        SHA_B,
        outcome,
        T1,
        model_version_id="model-1",
        completed_at=T2,
        notes="frozen result",
    )


def _append_repeat_bundle(
    registry: ScientificRegistry,
    foundation: dict[str, object],
    *,
    bundle_id: str = "eval-repeat",
    bundle_sha256: str = SHA_A,
    created_at: str = T3,
) -> tuple[EvaluationBundleRef, ScientificEvidenceRef]:
    bundle = replace(
        foundation["bundle"],
        evaluation_bundle_id=bundle_id,
        bundle_sha256=bundle_sha256,
        created_at=created_at,
    )
    registry.append(bundle)
    stored = registry.get("EvaluationBundle", bundle_id)
    assert stored is not None
    return bundle, ScientificEvidenceRef(
        "EvaluationBundle",
        bundle_id,
        stored.record_sha256,
    )


def _promotion_evidence(
    *,
    experiment_id: str,
    strategy_id: str,
    model_id: str,
    bundle_id: str,
    dataset_id: str,
    protocol_id: str,
    bundle_sha: str,
    evidence_id: str,
    created_at: str = T3,
    holdout_access_id: str | None = None,
    practical: str = "0.1",
    interval_low: str = "0.05",
    interval_high: str = "0.15",
    effective_n: int = 5,
    minimum_n: int = 3,
    validity=PromotionEvidenceValidity.ELIGIBLE,
    consumed: bool = False,
    guardrails: bool = True,
    rollback_identity: str = "strategy-previous",
) -> PromotionEvidence:
    import hashlib
    payload = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "research_protocol_id": protocol_id,
        "research_question_id": "question-1",
        "hypothesis_id": "hypothesis-1",
        "candidate_strategy_version_id": strategy_id,
        "candidate_model_version_id": model_id,
        "evaluation_bundle_id": bundle_id,
        "evaluation_bundle_sha256": bundle_sha,
        "dataset_snapshot_id": dataset_id,
        "holdout_access_id": holdout_access_id or promotion_holdout_access_id(
            research_protocol_id=protocol_id,
            dataset_manifest_sha256=SHA_A,
            source_identity="lawful-provider:fixture",
            license_identity="license-evidence:v1",
            confirmation_trial_family_id=f"{protocol_id}:confirmation-trial-family",
        ),
        "confirmation_trial_family_id": f"{protocol_id}:confirmation-trial-family",
        "estimand": "roi",
        "direction": PromotionEvidenceDirection.LOWER_IS_BETTER.value,
        "cohort_id": dataset_id,
        "effective_sample_size": effective_n,
        "minimum_effective_sample_size": minimum_n,
        "effect_interval_low": interval_low,
        "effect_interval_high": interval_high,
        "practical_improvement": practical,
        "guardrails_passed": guardrails,
        "validity": validity.value,
        "holdout_consumed": consumed,
        "stopping_rule_sha256": hashlib.sha256(b"one final evaluation").hexdigest(),
        "multiple_comparison_control_sha256": hashlib.sha256(b"single frozen primary metric").hexdigest(),
        "rollback_identity": rollback_identity,
        "uncertainty_method": "bootstrap intervals",
        "created_at": created_at,
    }
    canonical_id = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    fields = {key: value for key, value in payload.items() if key != "schema_version"}
    fields["direction"] = PromotionEvidenceDirection(payload["direction"])
    fields["validity"] = PromotionEvidenceValidity(payload["validity"])
    return PromotionEvidence(canonical_id, **fields)


def test_restart_preserves_negative_memory_and_blocks_duplicate_fingerprint(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    _foundation(registry)
    experiment = _experiment()
    registry.append(experiment)
    registry.append(
        Postmortem(
            "postmortem-1",
            "experiment-1",
            ResearchOutcome.NEGATIVE,
            "No primary-metric improvement.",
            ("new lawful dataset snapshot",),
            T3,
        )
    )

    reopened = ScientificRegistry(path)
    matches = reopened.find_experiment_fingerprint(experiment.fingerprint)
    assert [entry.record_id for entry in matches] == ["experiment-1"]
    assert matches[0].payload["outcome"] == "NEGATIVE"
    assert reopened.get("Postmortem", "postmortem-1") is not None

    repeat = replace(experiment, experiment_id="experiment-2")
    with pytest.raises(DuplicateExperimentFingerprintError):
        reopened.append(repeat)



def test_positive_only_explicit_repeat_survives_restart_without_negative_provenance(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    _foundation(registry)
    experiment = _experiment(outcome=ResearchOutcome.POSITIVE)
    registry.append(experiment)

    repeat = replace(
        experiment,
        experiment_id="experiment-positive-repeat",
        created_at=T3,
        completed_at=T3,
    )
    registry.append(repeat, allow_repeat_experiment=True)

    reopened = ScientificRegistry(path)
    matches = reopened.find_experiment_fingerprint(experiment.fingerprint)
    assert [entry.record_id for entry in matches] == [
        "experiment-1",
        "experiment-positive-repeat",
    ]
    assert all("repeat_of_experiment_id" not in entry.payload for entry in matches)


def test_negative_repeat_requires_durable_postmortem_provenance(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    foundation = _foundation(registry)
    experiment = _experiment()
    registry.append(experiment)

    repeat = replace(
        experiment,
        experiment_id="experiment-2",
        created_at=T3,
        completed_at=T3,
    )
    with pytest.raises(DuplicateExperimentFingerprintError, match="durable repeat provenance"):
        registry.append(repeat, allow_repeat_experiment=True)

    postmortem = Postmortem(
        "postmortem-repeat",
        "experiment-1",
        ResearchOutcome.NEGATIVE,
        "No primary-metric improvement.",
        (RetestCondition.NEW_EVALUATION_BUNDLE.value,),
        T3,
    )
    registry.append(postmortem)

    with pytest.raises(DuplicateExperimentFingerprintError, match="durable repeat provenance"):
        registry.append(repeat, allow_repeat_experiment=True)

    _, evidence_ref = _append_repeat_bundle(registry, foundation)
    caller_authored = replace(
        repeat,
        evaluation_bundle_id="eval-repeat",
        repeat_of_experiment_id="experiment-1",
        repeat_postmortem_id="postmortem-repeat",
        retest_condition=RetestCondition.NEW_EVALUATION_BUNDLE.value,
        repeat_evidence=(evidence_ref,),
    )
    with pytest.raises(
        DuplicateExperimentFingerprintError,
        match="product-issued EvaluationBundle authority",
    ):
        registry.append(caller_authored, allow_repeat_experiment=True)

    # The rejected attempt cannot become durable history, and restart must preserve
    # the original negative-result memory rather than silently accepting the repeat.
    reopened = ScientificRegistry(path)
    assert reopened.get("Experiment", "experiment-2") is None
    matches = reopened.find_experiment_fingerprint(experiment.fingerprint)
    assert [entry.record_id for entry in matches] == ["experiment-1"]
    assert reopened.get("Postmortem", "postmortem-repeat") is not None


def test_negative_repeat_rejects_wrong_postmortem_condition_or_time(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    _foundation(registry)
    experiment = _experiment()
    registry.append(experiment)

    with pytest.raises(DuplicateExperimentFingerprintError, match="missing experiment"):
        registry.append(
            Postmortem(
                "postmortem-missing",
                "does-not-exist",
                ResearchOutcome.NEGATIVE,
                "No result.",
                ("replicate",),
                T3,
            )
        )
    with pytest.raises(DuplicateExperimentFingerprintError, match="classification"):
        registry.append(
            Postmortem(
                "postmortem-wrong-class",
                "experiment-1",
                ResearchOutcome.HARMFUL,
                "Wrong classification.",
                ("replicate",),
                T3,
            )
        )
    with pytest.raises(DuplicateExperimentFingerprintError, match="predate"):
        registry.append(
            Postmortem(
                "postmortem-too-early",
                "experiment-1",
                ResearchOutcome.NEGATIVE,
                "Premature.",
                ("replicate",),
                T1,
            )
        )

    registry.append(
        Postmortem(
            "postmortem-valid",
            "experiment-1",
            ResearchOutcome.NEGATIVE,
            "Replication is permitted only under the frozen condition.",
            (RetestCondition.NEW_EVALUATION_BUNDLE.value,),
            T3,
        )
    )
    repeat = replace(
        experiment,
        experiment_id="experiment-2",
        created_at=T3,
        completed_at=T3,
        repeat_of_experiment_id="experiment-1",
        repeat_postmortem_id="postmortem-valid",
        retest_condition="different condition",
        repeat_evidence=(ScientificEvidenceRef("EvaluationBundle", "missing", SHA_A),),
    )
    with pytest.raises(DuplicateExperimentFingerprintError, match="retest_condition"):
        registry.append(repeat, allow_repeat_experiment=True)

    future_repeat = replace(
        repeat,
        retest_condition=RetestCondition.NEW_EVALUATION_BUNDLE.value,
        created_at=T2,
        completed_at=T3,
    )
    with pytest.raises(DuplicateExperimentFingerprintError, match="predate its authorizing postmortem"):
        registry.append(future_repeat, allow_repeat_experiment=True)


def test_negative_repeat_rejects_invented_wrong_digest_future_and_unchanged_bundle_evidence(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    experiment = _experiment()
    registry.append(experiment)
    registry.append(
        Postmortem(
            "postmortem-evidence",
            "experiment-1",
            ResearchOutcome.NEGATIVE,
            "Repeat only after a new durable evaluation exists.",
            (RetestCondition.NEW_EVALUATION_BUNDLE.value,),
            T2,
        )
    )
    base = replace(
        experiment,
        experiment_id="experiment-2",
        created_at=T3,
        completed_at=T3,
        repeat_of_experiment_id="experiment-1",
        repeat_postmortem_id="postmortem-evidence",
        retest_condition=RetestCondition.NEW_EVALUATION_BUNDLE.value,
    )

    invented = replace(
        base,
        evaluation_bundle_id="eval-missing",
        repeat_evidence=(
            ScientificEvidenceRef("EvaluationBundle", "eval-missing", SHA_A),
        ),
    )
    with pytest.raises(
        DuplicateExperimentFingerprintError,
        match="missing durable scientific record",
    ):
        registry.append(invented, allow_repeat_experiment=True)

    _, future_ref = _append_repeat_bundle(
        registry,
        foundation,
        bundle_id="eval-future",
        bundle_sha256=SHA_A,
        created_at="2026-01-05T00:00:00+00:00",
    )
    future = replace(
        base,
        evaluation_bundle_id="eval-future",
        repeat_evidence=(future_ref,),
    )
    with pytest.raises(
        DuplicateExperimentFingerprintError,
        match="not available before repeat creation",
    ):
        registry.append(future, allow_repeat_experiment=True)

    _, exact_ref = _append_repeat_bundle(
        registry,
        foundation,
        bundle_id="eval-repeat",
        bundle_sha256=SHA_A,
        created_at=T3,
    )
    wrong_digest = "0" * 64
    if wrong_digest == exact_ref.record_sha256:
        wrong_digest = "1" * 64
    forged = replace(
        base,
        evaluation_bundle_id="eval-repeat",
        repeat_evidence=(
            ScientificEvidenceRef("EvaluationBundle", "eval-repeat", wrong_digest),
        ),
    )
    with pytest.raises(
        DuplicateExperimentFingerprintError,
        match="digest mismatch",
    ):
        registry.append(forged, allow_repeat_experiment=True)

    original_bundle = registry.get("EvaluationBundle", "eval-1")
    assert original_bundle is not None
    unchanged = replace(
        base,
        evaluation_bundle_id="eval-1",
        repeat_evidence=(
            ScientificEvidenceRef(
                "EvaluationBundle",
                "eval-1",
                original_bundle.record_sha256,
            ),
        ),
    )
    with pytest.raises(
        DuplicateExperimentFingerprintError,
        match="new durable EvaluationBundle",
    ):
        registry.append(unchanged, allow_repeat_experiment=True)


def test_negative_repeat_rejects_same_bundle_content_under_new_identity(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    experiment = _experiment()
    registry.append(experiment)
    registry.append(
        Postmortem(
            "postmortem-content",
            "experiment-1",
            ResearchOutcome.NEGATIVE,
            "Require a machine-verifiable new evaluation before repeat.",
            (
                RetestCondition.NEW_EVALUATION_BUNDLE.value,
                "independent human replication",
            ),
            T2,
        )
    )
    original = foundation["bundle"]
    _, evidence_ref = _append_repeat_bundle(
        registry,
        foundation,
        bundle_id="eval-renamed-only",
        bundle_sha256=original.bundle_sha256,
        created_at=T3,
    )
    repeat = replace(
        experiment,
        experiment_id="experiment-2",
        evaluation_bundle_id="eval-renamed-only",
        created_at=T3,
        completed_at=T3,
        repeat_of_experiment_id="experiment-1",
        repeat_postmortem_id="postmortem-content",
        retest_condition=RetestCondition.NEW_EVALUATION_BUNDLE.value,
        repeat_evidence=(evidence_ref,),
    )
    with pytest.raises(
        DuplicateExperimentFingerprintError,
        match="materially changed evidence",
    ):
        registry.append(repeat, allow_repeat_experiment=True)

    _, material_ref = _append_repeat_bundle(
        registry,
        foundation,
        bundle_id="eval-material",
        bundle_sha256=SHA_A,
        created_at=T3,
    )
    non_mechanical = replace(
        repeat,
        evaluation_bundle_id="eval-material",
        retest_condition="independent human replication",
        repeat_evidence=(material_ref,),
    )
    with pytest.raises(
        DuplicateExperimentFingerprintError,
        match="mechanically supported",
    ):
        registry.append(non_mechanical, allow_repeat_experiment=True)


def test_conflicting_identity_rejected_but_exact_replay_is_idempotent(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    question = ResearchQuestion("q", "Frozen question", SHA_A, T0)
    first_hash = registry.append(question)
    assert registry.append(question) == first_hash

    with pytest.raises(ConflictingScientificRecordError):
        registry.append(replace(question, statement="Changed question"))


def test_causal_lookup_honors_availability_and_outcome_reveal(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    registry.append(
        DatasetSnapshot(
            "dataset-hidden",
            SHA_A,
            "source",
            "license",
            T1,
            T0,
            outcome_reveal_after=T2,
        )
    )
    assert registry.causal_records("DatasetSnapshot", as_of=T1) == ()
    visible = registry.causal_records("DatasetSnapshot", as_of=T2)
    assert [entry.record_id for entry in visible] == ["dataset-hidden"]


def test_promotion_fails_closed_then_blocks_reused_confirmation_holdout(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    experiment = _experiment(outcome=ResearchOutcome.POSITIVE)
    registry.append(experiment)

    missing = PromotionDecision(
        "promotion-missing",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-missing",
        SHA_D,
        T3,
        candidate_model_version_id="model-1",
    )
    with pytest.raises(PromotionEvidenceError):
        registry.record_promotion(missing)

    evidence1 = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="promotion-1-evidence",
        rollback_identity="NONE",
    )
    registry.append(evidence1)
    promote = replace(
        missing,
        promotion_decision_id="promotion-1",
        evaluation_bundle_id="eval-1",
        evaluation_bundle_sha256=foundation["bundle"].bundle_sha256,
        promotion_evidence_id=evidence1.promotion_evidence_id,
    )
    registry.record_promotion(promote)
    assert registry.champion_strategy(as_of=T3) == "strategy-1"

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
        foundation["protocol"].protocol_sha256,
        (SHA_B,),
        T3,
        evaluated_strategy_version_id="strategy-2",
        evaluated_model_version_id="model-1",
        effective_sample_size=5,
        effect_interval_low="0.05",
        effect_interval_high="0.15",
        practical_improvement="0.1",
    )
    experiment2 = replace(
        experiment,
        experiment_id="experiment-2",
        strategy_version_id="strategy-2",
        evaluation_bundle_id="eval-2",
        completed_at=T3,
    )
    for record in (strategy2, bundle2, experiment2):
        registry.append(record)
    evidence2 = _promotion_evidence(
        experiment_id="experiment-2",
        strategy_id="strategy-2",
        model_id="model-1",
        bundle_id="eval-2",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=bundle2.bundle_sha256,
        evidence_id="promotion-2-evidence",
        rollback_identity="strategy-1",
    )
    registry.append(evidence2)
    promote2 = PromotionDecision(
        "promotion-2",
        PromotionAction.PROMOTE,
        "strategy-2",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-2",
        bundle2.bundle_sha256,
        T3,
        predecessor_strategy_version_id="strategy-1",
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence2.promotion_evidence_id,
    )
    with pytest.raises(
        PromotionEvidenceError,
        match="confirmation holdout access has already been consumed",
    ):
        registry.record_promotion(promote2)
    assert registry.champion_strategy(as_of=T3) == "strategy-1"

def test_promotion_rejects_forged_effect_interval_against_durable_evaluation(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))

    forged = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="forged-effect-interval",
        rollback_identity="NONE",
        interval_low="0.06",
        interval_high="0.16",
        practical="0.11",
    )
    registry.append(forged)
    decision = PromotionDecision(
        "promotion-forged-effect",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        SHA_D,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=forged.promotion_evidence_id,
    )

    with pytest.raises(
        PromotionEvidenceError,
        match="effect interval low does not match durable evaluation",
    ):
        registry.record_promotion(decision)

def test_reproducibility_bundle_is_deterministic_reference_only(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    _foundation(registry)
    registry.append(_experiment())

    first = registry.reproducibility_bundle("experiment-1")
    second = registry.reproducibility_bundle("experiment-1")
    assert first == second
    assert first["bundle_sha256"] == second["bundle_sha256"]
    assert first["truth"] == {
        "raw_dataset_copied": False,
        "promotion_claim": False,
        "real_money_execution": False,
    }
    assert "DatasetSnapshot" in first["references"]
    assert "dataset_bytes" not in first and "raw_rows" not in first

    target = tmp_path / "repro.json"
    assert registry.export_reproducibility_bundle("experiment-1", target) == first["bundle_sha256"]
    assert json.loads(target.read_text(encoding="utf-8")) == first


def test_active_workspace_writer_blocks_second_writer_without_corruption(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    with WorkspaceEconomicLock(tmp_path):
        with pytest.raises(WorkspaceEconomicLockBusyError):
            registry.append(ResearchQuestion("q", "question", SHA_A, T0))

    reopened = ScientificRegistry(path)
    assert reopened.get("ResearchQuestion", "q") is None


def test_registry_detects_tampering_on_restart(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    registry.append(ResearchQuestion("q", "question", SHA_A, T0))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["records"][0]["payload"]["statement"] = "tampered"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="digest mismatch"):
        ScientificRegistry(path)



def test_restart_revalidates_promotion_candidate_against_durable_evaluation(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    foundation = _foundation(registry)
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="restart-binding",
        rollback_identity="NONE",
    )
    registry.append(evidence)
    registry.append(
        StrategyVersion(
            "strategy-shadow",
            "canonical-strategy",
            SHA_C,
            SHA_D,
            SHA_B,
            T3,
            model_version_id="model-1",
            predecessor_strategy_version_id="strategy-1",
        )
    )
    registry.record_promotion(
        PromotionDecision(
            "promotion-restart-binding",
            PromotionAction.PROMOTE,
            "strategy-1",
            "protocol-1",
            foundation["protocol"].protocol_sha256,
            "eval-1",
            foundation["bundle"].bundle_sha256,
            T3,
            candidate_model_version_id="model-1",
            promotion_evidence_id=evidence.promotion_evidence_id,
        )
    )
    assert registry.champion_strategy(as_of=T3) == "strategy-1"

    raw = json.loads(path.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in raw["records"]
        if item["record_type"] == "PromotionDecision"
        and item["record_id"] == "promotion-restart-binding"
    )
    entry["payload"]["candidate_strategy_version_id"] = "strategy-shadow"
    envelope = {
        "record_type": entry["record_type"],
        "record_id": entry["record_id"],
        "available_at": entry["available_at"],
        "payload": entry["payload"],
    }
    entry["record_sha256"] = hashlib.sha256(
        json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    path.write_text(
        json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        PromotionEvidenceError,
        match="evaluation strategy mismatch",
    ):
        ScientificRegistry(path)


@pytest.mark.parametrize("bad_schema_version", [True, 1.0])
def test_registry_rejects_noncanonical_schema_version_types(
    tmp_path, bad_schema_version
):
    path = tmp_path / "scientific_registry.json"
    ScientificRegistry.initialize_pristine(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = bad_schema_version
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version mismatch"):
        ScientificRegistry(path)


def test_protocol_record_reuses_existing_scientific_binding_hash(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    protocol = ResearchProtocol(_binding(), SHA_C, SHA_D, SHA_A, T0)
    registry.append(protocol)
    stored = registry.get("ResearchProtocol", "protocol-1")
    assert stored is not None
    assert stored.payload["protocol_sha256"] == _binding().binding_sha256
    assert stored.payload["binding"] == _binding().canonical_dict()


def test_final_experiment_requires_completion_and_valid_time_order():
    with pytest.raises(ValueError, match="completed_at is required"):
        replace(_experiment(), completed_at=None)

    with pytest.raises(ValueError, match="must not precede"):
        replace(_experiment(), created_at=T2, completed_at=T1)


def test_promotion_rejects_cross_dataset_model_experiment_lineage(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    registry.append(
        DatasetSnapshot(
            "dataset-2",
            SHA_B,
            "lawful-provider:fixture-2",
            "license-evidence:v1",
            T1,
            T0,
            outcome_reveal_after=T1,
        )
    )
    registry.append(
        replace(
            _experiment(outcome=ResearchOutcome.POSITIVE),
            dataset_snapshot_id="dataset-2",
        )
    )
    decision = PromotionDecision(
        "promotion-cross-dataset",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        SHA_D,
        T3,
        candidate_model_version_id="model-1",
    )

    with pytest.raises(PromotionEvidenceError, match="dataset lineage mismatch"):
        registry.record_promotion(decision)


def test_promotion_rejects_evidence_not_available_at_decision_time(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    early = PromotionDecision(
        "promotion-too-early",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        SHA_D,
        T1,
        candidate_model_version_id="model-1",
    )

    with pytest.raises(PromotionEvidenceError, match="not available at decision time"):
        registry.record_promotion(early)


def test_backdated_promotion_evidence_is_rejected_before_causal_visibility(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    backdated = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="promotion-backdated-evidence",
        rollback_identity="NONE",
        created_at=T1,
    )

    with pytest.raises(
        PromotionEvidenceError,
        match="availability precedes referenced evaluation",
    ):
        registry.append(backdated)

    assert registry.get("PromotionEvidence", backdated.promotion_evidence_id) is None
    assert registry.causal_records("PromotionEvidence", as_of=T1) == ()

    decision = PromotionDecision(
        "promotion-backdated-evidence",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=backdated.promotion_evidence_id,
    )
    with pytest.raises(PromotionEvidenceError, match="missing PromotionEvidence"):
        registry.record_promotion(decision)


def test_direct_promotion_append_cannot_bypass_evidence_validation(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    decision = PromotionDecision(
        "promotion-bypass",
        PromotionAction.PROMOTE,
        "strategy-missing",
        "protocol-missing",
        SHA_A,
        "eval-missing",
        SHA_B,
        T3,
    )

    with pytest.raises(PromotionEvidenceError, match="record_promotion"):
        registry.append(decision)


def test_champion_history_orders_mixed_timezone_offsets_by_instant(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    first_experiment = _experiment(outcome=ResearchOutcome.POSITIVE)
    registry.append(first_experiment)
    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="promotion-offset-evidence",
        rollback_identity="NONE",
        created_at="2026-01-03T23:00:00+00:00",
    )
    registry.append(evidence)
    first = PromotionDecision(
        "promotion-offset-earlier",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        "2026-01-04T01:30:00+02:00",
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )
    registry.record_promotion(first)

    assert registry.champion_strategy(as_of="2026-01-03T23:29:59+00:00") is None
    assert registry.champion_strategy(as_of="2026-01-03T23:30:00+00:00") == "strategy-1"
    assert registry.champion_strategy(as_of="2026-01-04T01:00:00+00:00") == "strategy-1"

@pytest.mark.parametrize(
    "rule_text",
    [
        (
            '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi",'
            '"primary_metric":"roi","minimum_improvement":0.05,'
            '"minimum_effective_sample_size":3,"protective_metric_maxima":[],'
            '"metric_direction":"lower_is_better"}'
        ),
        (
            '{"kind":"autosport-promotion-rule-v1","primary_metric":"roi",'
            '"minimum_improvement":0.05,"minimum_effective_sample_size":3,'
            '"protective_metric_maxima":[NaN],'
            '"metric_direction":"lower_is_better"}'
        ),
    ],
)
def test_promotion_rejects_noncanonical_frozen_rule_json(tmp_path, rule_text):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    foundation = _foundation_with_binding(
        registry,
        promotion_rule=rule_text,
        effective_sample_size=3,
    )
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="noncanonical-frozen-rule",
        practical="0.1",
        interval_low="0.1",
        interval_high="0.15",
        effective_n=3,
        minimum_n=3,
        rollback_identity="NONE",
    )
    registry.append(evidence)
    decision = PromotionDecision(
        "promotion-noncanonical-frozen-rule",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )

    with pytest.raises(PromotionEvidenceError, match="canonical JSON"):
        registry.record_promotion(decision)


def test_promotion_rejects_positive_but_below_frozen_minimum_improvement(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    rule_text = _frozen_promotion_rule_text(minimum_improvement=0.05, minimum_effective_sample_size=3)
    foundation = _foundation_with_binding(
        registry,
        promotion_rule=rule_text,
        effective_sample_size=3,
        effect_interval_low="0.04",
        effect_interval_high="0.06",
        practical_improvement="0.04",
    )
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="below-threshold",
        practical="0.04",
        interval_low="0.04",
        interval_high="0.06",
        effective_n=3,
        minimum_n=3,
        rollback_identity="NONE",
    )
    registry.append(evidence)
    decision = PromotionDecision(
        "promotion-below-threshold",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )
    with pytest.raises(PromotionEvidenceError, match="frozen minimum"):
        registry.record_promotion(decision)


def test_promotion_rejects_caller_selected_sample_floor(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    rule_text = _frozen_promotion_rule_text(minimum_improvement=0.05, minimum_effective_sample_size=3)
    foundation = _foundation_with_binding(
        registry,
        promotion_rule=rule_text,
        effective_sample_size=3,
    )
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="caller-selected-floor",
        practical="0.1",
        interval_low="0.1",
        interval_high="0.12",
        effective_n=3,
        minimum_n=2,
        rollback_identity="NONE",
    )
    registry.append(evidence)
    decision = PromotionDecision(
        "promotion-caller-selected-floor",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )
    with pytest.raises(PromotionEvidenceError, match="minimum effective sample size is not frozen"):
        registry.record_promotion(decision)


def test_promotion_rejects_caller_inflated_effective_sample_size(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    rule_text = _frozen_promotion_rule_text(
        minimum_improvement=0.05,
        minimum_effective_sample_size=3,
    )
    foundation = _foundation_with_binding(
        registry,
        promotion_rule=rule_text,
        effective_sample_size=2,
    )
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="caller-inflated-effective-n",
        practical="0.1",
        interval_low="0.1",
        interval_high="0.12",
        effective_n=3,
        minimum_n=3,
        rollback_identity="NONE",
    )
    registry.append(evidence)
    decision = PromotionDecision(
        "promotion-caller-inflated-effective-n",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )
    with pytest.raises(
        PromotionEvidenceError,
        match="effective sample size does not match durable evaluation",
    ):
        registry.record_promotion(decision)


def test_promotion_holdout_identity_is_stable_across_dataset_snapshot_renames():
    first = promotion_holdout_access_id(
        research_protocol_id="protocol-1",
        dataset_manifest_sha256=SHA_A,
        source_identity="lawful-provider:fixture",
        license_identity="license-evidence:v1",
        confirmation_trial_family_id="protocol-1:confirmation-trial-family",
    )
    second = promotion_holdout_access_id(
        research_protocol_id="protocol-1",
        dataset_manifest_sha256=SHA_A,
        source_identity="lawful-provider:fixture",
        license_identity="license-evidence:v1",
        confirmation_trial_family_id="protocol-1:confirmation-trial-family",
    )
    assert first == second
    assert len(first) == 64

def test_promotion_evidence_identity_and_strict_improvement(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation_with_binding(
        registry,
        promotion_rule=_frozen_promotion_rule_text(
            minimum_improvement=0.0,
            minimum_effective_sample_size=3,
        ),
        effect_interval_low="0",
        effect_interval_high="0",
        practical_improvement="0",
    )
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))

    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="strict-zero",
        rollback_identity="NONE",
        practical="0",
        interval_low="0",
        interval_high="0",
    )
    registry.append(evidence)
    decision = PromotionDecision(
        "promotion-strict-zero",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )
    with pytest.raises(PromotionEvidenceError, match="strictly positive"):
        registry.record_promotion(decision)

def test_promotion_rejects_reuse_of_consumed_evidence_and_holdout(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="consume",
        rollback_identity="NONE",
    )
    registry.append(evidence)
    decision = PromotionDecision(
        "promotion-consume-1",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )
    registry.record_promotion(decision)
    with pytest.raises(PromotionEvidenceError):
        registry.record_promotion(replace(decision, promotion_decision_id="promotion-consume-2"))


def test_promotion_rejects_disclosed_holdout_without_prior_decision(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))

    disclosed = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="disclosed-without-decision",
        created_at=T2,
        rollback_identity="NONE",
        practical="0.09",
        interval_low="0.04",
        interval_high="0.14",
    )
    registry.append(disclosed)

    current = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="current-after-disclosure",
        rollback_identity="NONE",
    )
    registry.append(current)
    decision = PromotionDecision(
        "promotion-after-disclosure",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=current.promotion_evidence_id,
    )

    with pytest.raises(
        PromotionEvidenceError,
        match="consumed or disclosed",
    ):
        registry.record_promotion(decision)


def test_promotion_rejects_preconsumed_confirmation_holdout(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    foundation = _foundation(registry)
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="preconsumed",
        rollback_identity="NONE",
        consumed=True,
    )
    registry.append(evidence)
    decision = PromotionDecision(
        "promotion-consumed",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )
    with pytest.raises(PromotionEvidenceError, match="unconsumed confirmation holdout"):
        registry.record_promotion(decision)



def _stored_scientific_ref(
    registry: ScientificRegistry,
    record_type: str,
    record_id: str,
) -> ScientificEvidenceRef:
    stored = registry.get(record_type, record_id)
    assert stored is not None
    return ScientificEvidenceRef(record_type, record_id, stored.record_sha256)


def _ablation_authority(
    registry: ScientificRegistry,
    foundation: dict[str, object],
    *,
    authority_id: str = "ablation-authority-1",
    kind: AblationAuthorityKind = AblationAuthorityKind.FROZEN_REPLAY_COUNTERFACTUAL,
    created_at: str = T3,
    observation_evidence: ScientificEvidenceRef | None = None,
    supporting_evidence: tuple[ScientificEvidenceRef, ...] = (),
    execution_receipt_sha256: str | None = None,
    assumptions: tuple[str, ...] = (),
) -> AblationAuthorityEvidence:
    protocol_entry = registry.get("ResearchProtocol", "protocol-1")
    dataset_entry = registry.get("DatasetSnapshot", "dataset-1")
    assert protocol_entry is not None
    assert dataset_entry is not None
    dataset = foundation["dataset"]
    assert isinstance(dataset, DatasetSnapshot)
    holdout_access_sha256 = promotion_holdout_access_id(
        research_protocol_id="protocol-1",
        dataset_manifest_sha256=dataset.manifest_sha256,
        source_identity=dataset.source_identity,
        license_identity=dataset.license_identity,
        confirmation_trial_family_id="protocol-1:confirmation-trial-family",
    )
    return AblationAuthorityEvidence(
        ablation_authority_id=authority_id,
        authority_kind=kind,
        research_protocol_id="protocol-1",
        protocol_sha256=foundation["protocol"].protocol_sha256,
        research_protocol_record_sha256=protocol_entry.record_sha256,
        scope_id="soccer:provider-a:h2h",
        dataset_snapshot_id="dataset-1",
        dataset_manifest_sha256=dataset.manifest_sha256,
        dataset_snapshot_record_sha256=dataset_entry.record_sha256,
        confirmation_trial_family_id="protocol-1:confirmation-trial-family",
        holdout_access_sha256=holdout_access_sha256,
        causal_cutoff=T1,
        observation_evidence=(
            observation_evidence
            or _stored_scientific_ref(registry, "EvaluationBundle", "eval-1")
        ),
        supporting_evidence=supporting_evidence,
        execution_receipt_sha256=execution_receipt_sha256,
        assumptions=assumptions,
        created_at=created_at,
    )


def test_ablation_authority_requires_causal_recording_and_survives_restart(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)
    foundation = _foundation(registry)
    record = _ablation_authority(registry, foundation)

    with pytest.raises(ValueError, match="record_ablation_authority"):
        registry.append(record)

    digest = registry.record_ablation_authority(record)
    stored = registry.get("AblationAuthorityEvidence", record.record_id)
    assert stored is not None
    assert stored.record_sha256 == digest
    assert stored.payload["authority_kind"] == "FROZEN_REPLAY_COUNTERFACTUAL"
    assert stored.payload["holdout_access_sha256"] == record.holdout_access_sha256

    reopened = ScientificRegistry(path)
    reopened_record = reopened.get("AblationAuthorityEvidence", record.record_id)
    assert reopened_record is not None
    assert reopened_record.record_sha256 == digest
    assert reopened.record_ablation_authority(record) == digest


@pytest.mark.parametrize(
    "mutator, expected",
    (
        (
            lambda value: replace(value, research_protocol_record_sha256=SHA_B),
            "research protocol digest mismatch",
        ),
        (
            lambda value: replace(value, dataset_snapshot_record_sha256=SHA_B),
            "dataset snapshot digest mismatch",
        ),
        (
            lambda value: replace(value, holdout_access_sha256=SHA_B),
            "holdout identity mismatch",
        ),
        (
            lambda value: replace(value, causal_cutoff=T0),
            "causal cutoff mismatch",
        ),
        (
            lambda value: replace(
                value,
                observation_evidence=replace(
                    value.observation_evidence,
                    record_sha256=SHA_B,
                ),
            ),
            "observation evidence digest mismatch",
        ),
    ),
)
def test_ablation_authority_rejects_forged_durable_bindings(
    tmp_path,
    mutator,
    expected,
):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    foundation = _foundation(registry)
    record = mutator(_ablation_authority(registry, foundation))

    with pytest.raises(ValueError, match=expected):
        registry.record_ablation_authority(record)
    assert registry.get("AblationAuthorityEvidence", record.record_id) is None


def test_ablation_authority_rejects_future_evidence(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    foundation = _foundation(registry)
    future_bundle = replace(
        foundation["bundle"],
        evaluation_bundle_id="eval-future",
        bundle_sha256=SHA_A,
        created_at="2026-01-05T00:00:00+00:00",
    )
    registry.append(future_bundle)
    future_ref = _stored_scientific_ref(
        registry,
        "EvaluationBundle",
        "eval-future",
    )
    record = _ablation_authority(
        registry,
        foundation,
        created_at=T3,
        observation_evidence=future_ref,
    )

    with pytest.raises(ValueError, match="observation evidence was not causally available"):
        registry.record_ablation_authority(record)


def test_ablation_authority_simulation_assumptions_are_kind_bound(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    foundation = _foundation(registry)

    with pytest.raises(ValueError, match="requires explicit assumptions"):
        _ablation_authority(
            registry,
            foundation,
            kind=AblationAuthorityKind.SIMULATED_COUNTERFACTUAL,
        )
    with pytest.raises(ValueError, match="non-simulated"):
        _ablation_authority(
            registry,
            foundation,
            assumptions=("simulator=v1",),
        )
    simulated = _ablation_authority(
        registry,
        foundation,
        authority_id="ablation-authority-simulated",
        kind=AblationAuthorityKind.SIMULATED_COUNTERFACTUAL,
        assumptions=("model=causal-v1", "seed=7"),
    )
    digest = registry.record_ablation_authority(simulated)
    assert registry.get("AblationAuthorityEvidence", simulated.record_id).record_sha256 == digest


def test_ablation_authority_rejects_recursive_registry_authority_reference(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    foundation = _foundation(registry)
    recursive_ref = ScientificEvidenceRef(
        "AblationAuthorityEvidence",
        "other-ablation-authority",
        SHA_A,
    )

    with pytest.raises(ValueError, match="another ablation authority|recursively"):
        _ablation_authority(
            registry,
            foundation,
            observation_evidence=recursive_ref,
        )
