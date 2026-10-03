from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal

import pytest

import autosport._scientific_registry_read_authority as registry_read_authority

from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.research_multiplicity import (
    ExperimentFamilyMember,
    ExperimentFamilyPlan,
    MetricDirection,
    MultiplicityControlKind,
    SequentialDecision,
    SequentialLookEvidence,
    SequentialMultiplicityEvidenceStore,
)
from autosport.scientific_registry import (
    DatasetSnapshot,
    EvaluationBundleRef,
    ExperimentRecord,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    PromotionEvidence,
    PromotionEvidenceDirection,
    PromotionEvidenceValidity,
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
T4 = "2026-09-20T04:00:00Z"


def _payload_sha(record) -> str:
    canonical = json.dumps(record.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _question() -> ResearchQuestion:
    return ResearchQuestion("question-1", "Does the frozen candidate improve the primary metric?", SHA_A, T0)


def _hypothesis() -> Hypothesis:
    return Hypothesis("hypothesis-1", "question-1", "Candidate improves frozen ROI.", "holdout ROI improves", "ROI fails to improve or a guardrail regresses", "roi", ("max_drawdown",), T0)


def _binding(question: ResearchQuestion, hypothesis: Hypothesis) -> ScientificProtocolBinding:
    return ScientificProtocolBinding(
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
        promotion_rule=json.dumps({"kind": "autosport-promotion-rule-v1", "primary_metric": "roi", "minimum_improvement": 0.01, "minimum_effective_sample_size": 3, "protective_metric_maxima": [], "metric_direction": "higher_is_better"}, sort_keys=True, separators=(",", ":")),
        expected_artifacts=("evaluation bundle", "decision"),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )


def _candidate() -> TrialCandidateLineage:
    return TrialCandidateLineage(strategy_version_id="strategy-1", model_version_id="model-1", dataset_snapshot_id="dataset-1", feature_set_id="features-1", seed=7, config_sha256=SHA_B)


def _foundation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    TrialFamilyAccountingStore.initialize_workspace(workspace)
    # ScientificRegistry publication and trial-family accounting share one
    # canonical workspace root-selection decision. The suite-wide fixture supplies
    # an isolated default monotonic root, so do not introduce a second explicit
    # root when this helper is nested inside the same test.
    authority_root = None
    registry = ScientificRegistry.initialize_pristine(workspace / "scientific.json")
    question = _question()
    hypothesis = _hypothesis()
    binding = _binding(question, hypothesis)
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, SHA_A, T0)
    dataset = DatasetSnapshot("dataset-1", SHA_A, "lawful-provider:fixture", "license-evidence:v1", T1, T0, outcome_reveal_after=T1)
    features = FeatureSet("features-1", "v1", SHA_B, SHA_C, T0)
    model = ModelVersion("model-1", "fixture-model", SHA_A, SHA_C, SHA_D, "dataset-1", "features-1", "protocol-1", 7, SHA_B, T1)
    strategy = StrategyVersion("strategy-1", "canonical-strategy", SHA_C, SHA_D, SHA_B, T1, model_version_id="model-1")
    bundle = EvaluationBundleRef("eval-1", SHA_D, SHA_C, "dataset-1", protocol.protocol_sha256, (SHA_A, SHA_B), T2, evaluated_strategy_version_id="strategy-1", evaluated_model_version_id="model-1", effective_sample_size=5, effect_interval_low="0.01", effect_interval_high="0.1", practical_improvement="0.05")
    for record in (question, hypothesis, protocol, dataset, features, model, strategy):
        registry.append(record)
    candidate = _candidate()
    member = ExperimentFamilyMember(hypothesis_id=hypothesis.hypothesis_id, hypothesis_sha256=_payload_sha(hypothesis), semantic_variant_sha256=candidate.semantic_sha256, candidate_label="candidate display label")
    plan = ExperimentFamilyPlan(family_id="display-family-name", research_protocol_id=protocol.record_id, protocol_sha256=protocol.protocol_sha256, research_question_id=question.question_id, primary_metric="roi", direction=MetricDirection.HIGHER_IS_BETTER, control_kind=MultiplicityControlKind.FWER, method_id=ExperimentFamilyPlan.IMPLEMENTED_METHOD, stopping_rule=binding.stopping_rule, familywise_alpha=Decimal("0.05"), look_alpha_spend=(Decimal("0.01"), Decimal("0.01")), members=(member,), frozen_at=T0)
    store = TrialFamilyAccountingStore.initialize_pristine(workspace / "trial-family.json", registry, plan, workspace_root=workspace, authority_root=authority_root)
    return registry, binding, bundle, candidate, member, plan, store


def _experiment(*, experiment_id: str = "experiment-1", protocol_id: str = "protocol-1", outcome: ResearchOutcome = ResearchOutcome.NEGATIVE, config_sha256: str = SHA_B) -> ExperimentRecord:
    return ExperimentRecord(experiment_id, protocol_id, "dataset-1", "features-1", "strategy-1", "eval-1", 7, config_sha256, outcome, T1, model_version_id="model-1", completed_at=T3, notes="frozen result")


def _look(plan, member, bundle, experiment, *, index=1, p="0.001", observed_at=T3) -> SequentialLookEvidence:
    return SequentialLookEvidence(family_plan_sha256=plan.plan_sha256, member_authority_id=member.member_authority_id, hypothesis_id=member.hypothesis_id, experiment_id=experiment.experiment_id, evaluation_bundle_id=bundle.evaluation_bundle_id, evaluation_bundle_sha256=bundle.bundle_sha256, look_index=index, observed_p_value=Decimal(p), classification=experiment.outcome, observed_at=observed_at, candidate_label="renamable display label")


def _promotion_evidence(*, store: TrialFamilyAccountingStore, experiment: ExperimentRecord, bundle: EvaluationBundleRef) -> PromotionEvidence:
    payload = {
        "schema_version": 1, "experiment_id": experiment.experiment_id, "research_protocol_id": "protocol-1", "research_question_id": "question-1", "hypothesis_id": "hypothesis-1", "candidate_strategy_version_id": "strategy-1", "candidate_model_version_id": "model-1", "evaluation_bundle_id": bundle.evaluation_bundle_id, "evaluation_bundle_sha256": bundle.bundle_sha256, "dataset_snapshot_id": "dataset-1", "holdout_access_id": "holdout-1", "confirmation_trial_family_id": store.family.family_id, "estimand": "roi", "direction": PromotionEvidenceDirection.HIGHER_IS_BETTER.value, "cohort_id": "dataset-1", "effective_sample_size": 5, "minimum_effective_sample_size": 3, "effect_interval_low": "0.01", "effect_interval_high": "0.1", "practical_improvement": "0.05", "guardrails_passed": True, "validity": PromotionEvidenceValidity.ELIGIBLE.value, "holdout_consumed": True, "stopping_rule_sha256": store.family.stopping_rule_sha256, "multiple_comparison_control_sha256": store.family.multiple_comparison_control_sha256, "rollback_identity": "strategy-previous", "uncertainty_method": "bootstrap intervals", "created_at": T4,
    }
    evidence_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    fields = {key: value for key, value in payload.items() if key != "schema_version"}
    fields["direction"] = PromotionEvidenceDirection(fields["direction"])
    fields["validity"] = PromotionEvidenceValidity(fields["validity"])
    return PromotionEvidence(evidence_id, **fields)


def test_family_identity_binds_exact_protocol_and_native_multiplicity_contract(tmp_path):
    _, binding, _, _, _, plan, store = _foundation(tmp_path)
    assert store.family.research_protocol_id == "protocol-1"
    assert store.family.protocol_sha256 == plan.protocol_sha256
    assert store.family.multiplicity_contract_sha256 == plan.plan_sha256
    assert store.family.stopping_rule_sha256 == hashlib.sha256(binding.stopping_rule.encode()).hexdigest()
    assert store.family.multiple_comparison_control_sha256 == hashlib.sha256(binding.multiple_comparison_control.encode()).hexdigest()


def test_same_family_cannot_pristine_reset_at_sibling_path(tmp_path):
    registry, _, _, _, _, plan, store = _foundation(tmp_path)
    with pytest.raises(MonotonicAuthorityRollbackError):
        TrialFamilyAccountingStore.initialize_pristine(store.workspace_root / "sibling.json", registry, plan, workspace_root=store.workspace_root, authority_root=store.authority_root)


def test_alias_family_cannot_remint_same_semantic_member_budget(tmp_path):
    registry, _, _, _, _, plan, store = _foundation(tmp_path)
    alias = replace(plan, family_id="alias-name")
    with pytest.raises(ValueError, match="already enrolled"):
        TrialFamilyAccountingStore.initialize_pristine(store.workspace_root / "alias.json", registry, alias, workspace_root=store.workspace_root, authority_root=store.authority_root)


def test_family_resolution_rejects_plan_stopping_rule_drift(tmp_path):
    registry, _, _, _, _, plan, store = _foundation(tmp_path)
    bad = replace(plan, stopping_rule="different post-hoc stopping rule")
    with pytest.raises(ValueError, match="stopping rule"):
        TrialFamilyAccountingStore.initialize_pristine(store.workspace_root / "other-family.json", registry, bad, workspace_root=store.workspace_root, authority_root=store.authority_root)


def test_family_resolution_rejects_forged_member_hypothesis(tmp_path):
    registry, _, _, candidate, member, plan, store = _foundation(tmp_path)
    bad_member = ExperimentFamilyMember(hypothesis_id="forged-hypothesis", hypothesis_sha256=member.hypothesis_sha256, semantic_variant_sha256=candidate.semantic_sha256, candidate_label="forged member")
    bad = replace(plan, members=(bad_member,))
    with pytest.raises(ValueError, match="member hypothesis"):
        TrialFamilyAccountingStore.initialize_pristine(store.workspace_root / "forged-family.json", registry, bad, workspace_root=store.workspace_root, authority_root=store.authority_root)


def test_semantic_attempt_exact_retry_is_idempotent_but_conflict_fails_closed(tmp_path):
    _, _, _, candidate, member, _, store = _foundation(tmp_path)
    first = store.start_attempt(semantic_attempt_id="attempt-semantic-1", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    replay = store.start_attempt(semantic_attempt_id="attempt-semantic-1", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    assert replay == first
    with pytest.raises(ValueError, match="conflicting duplicate semantic"):
        store.start_attempt(semantic_attempt_id="attempt-semantic-1", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T2)


def test_exact_duplicate_start_converges_across_stale_interleaving(tmp_path, monkeypatch):
    _, _, _, candidate, member, _, store = _foundation(tmp_path)
    peer = TrialFamilyAccountingStore(store.path, workspace_root=store.workspace_root, authority_root=store.authority_root)
    original_append = TrialFamilyAccountingStore._append_event
    injected = False
    peer_attempt = None

    def interleaving_append(instance, kind, event_at, payload, **kwargs):
        nonlocal injected, peer_attempt
        if instance is store and kind == "ATTEMPT_STARTED" and not injected:
            injected = True
            peer_attempt = peer.start_attempt(semantic_attempt_id="concurrent-exact", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
        return original_append(instance, kind, event_at, payload, **kwargs)

    monkeypatch.setattr(TrialFamilyAccountingStore, "_append_event", interleaving_append)
    result = store.start_attempt(semantic_attempt_id="concurrent-exact", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    assert injected and peer_attempt is not None
    assert result == peer_attempt
    assert len(store.attempts()) == 1


def test_conflicting_duplicate_start_fails_closed_after_stale_interleaving(tmp_path, monkeypatch):
    _, _, _, candidate, member, _, store = _foundation(tmp_path)
    peer = TrialFamilyAccountingStore(store.path, workspace_root=store.workspace_root, authority_root=store.authority_root)
    original_append = TrialFamilyAccountingStore._append_event
    injected = False

    def interleaving_append(instance, kind, event_at, payload, **kwargs):
        nonlocal injected
        if instance is store and kind == "ATTEMPT_STARTED" and not injected:
            injected = True
            peer.start_attempt(semantic_attempt_id="concurrent-conflict", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T2)
        return original_append(instance, kind, event_at, payload, **kwargs)

    monkeypatch.setattr(TrialFamilyAccountingStore, "_append_event", interleaving_append)
    with pytest.raises(ValueError, match="conflicting duplicate semantic"):
        store.start_attempt(semantic_attempt_id="concurrent-conflict", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    assert injected
    attempts = store.attempts()
    assert len(attempts) == 1 and attempts[0].created_at == T2


def test_backdated_append_is_rejected_and_asof_remains_causal(tmp_path):
    _, _, _, candidate, member, _, store = _foundation(tmp_path)
    store.start_attempt(semantic_attempt_id="t1", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    store.start_attempt(semantic_attempt_id="t3", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T3)
    with pytest.raises(ValueError, match="must not precede durable event history"):
        store.start_attempt(semantic_attempt_id="late-t2", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T2)
    early = store.snapshot(as_of=T2)
    assert early.total_attempts == early.open_attempts == 1


def test_machine_authority_rejects_tail_truncation(tmp_path):
    _, _, _, candidate, member, _, store = _foundation(tmp_path)
    store.start_attempt(semantic_attempt_id="one", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    old_bytes = store.path.read_bytes()
    store.start_attempt(semantic_attempt_id="two", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T2)
    store.path.write_bytes(old_bytes)
    with pytest.raises(MonotonicAuthorityRollbackError):
        TrialFamilyAccountingStore(store.path, workspace_root=store.workspace_root, authority_root=store.authority_root)


def test_negative_completed_and_aborted_attempts_survive_restart_and_asof(tmp_path):
    registry, _, bundle, candidate, member, _, store = _foundation(tmp_path)
    completed = store.start_attempt(semantic_attempt_id="negative-attempt", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    aborted = store.start_attempt(semantic_attempt_id="aborted-attempt", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T2)
    early = store.snapshot(as_of=T2)
    assert (early.total_attempts, early.completed_attempts, early.aborted_attempts, early.open_attempts) == (2, 0, 0, 2)
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.NEGATIVE)
    registry.append(experiment)
    closed = store.complete_attempt(attempt_id=completed.attempt_id, experiment_id=experiment.experiment_id, registry=registry)
    assert closed.status is TrialAttemptStatus.COMPLETED and closed.outcome is ResearchOutcome.NEGATIVE
    stopped = store.abort_attempt(attempt_id=aborted.attempt_id, aborted_at=T3, reason="predeclared source became unavailable")
    assert stopped.status is TrialAttemptStatus.ABORTED
    final = TrialFamilyAccountingStore(store.path, workspace_root=store.workspace_root, authority_root=store.authority_root).snapshot(as_of=T4)
    assert (final.total_attempts, final.completed_attempts, final.aborted_attempts, final.negative) == (2, 1, 1, 1)


def test_completion_requires_durable_experiment_exact_protocol_and_candidate(tmp_path):
    registry, _, _, candidate, member, _, store = _foundation(tmp_path)
    attempt = store.start_attempt(semantic_attempt_id="candidate-attempt", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    with pytest.raises(ValueError, match="missing from ScientificRegistry"):
        store.complete_attempt(attempt_id=attempt.attempt_id, experiment_id="missing", registry=registry)
    wrong_protocol = _experiment(experiment_id="wrong-protocol", protocol_id="protocol-other")
    registry.append(wrong_protocol)
    with pytest.raises(ValueError, match="research_protocol_id"):
        store.complete_attempt(attempt_id=attempt.attempt_id, experiment_id=wrong_protocol.experiment_id, registry=registry)
    registry2, _, _, candidate2, member2, _, store2 = _foundation(tmp_path / "candidate-mismatch")
    attempt2 = store2.start_attempt(semantic_attempt_id="candidate-attempt", member_authority_id=member2.member_authority_id, candidate=candidate2, created_at=T1)
    wrong_candidate = _experiment(experiment_id="wrong-candidate", config_sha256=SHA_C)
    registry2.append(wrong_candidate)
    with pytest.raises(ValueError, match="config_sha256"):
        store2.complete_attempt(attempt_id=attempt2.attempt_id, experiment_id=wrong_candidate.experiment_id, registry=registry2)


def test_completion_rejects_foreign_evaluation_bundle_lineage(tmp_path):
    registry, _, _, candidate, member, plan, store = _foundation(tmp_path)
    attempt = store.start_attempt(semantic_attempt_id="foreign-bundle", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    foreign_bundle = EvaluationBundleRef("eval-foreign", SHA_A, SHA_C, "dataset-foreign", plan.protocol_sha256, (SHA_C, SHA_D), T2, evaluated_strategy_version_id="strategy-1", evaluated_model_version_id="model-1")
    registry.append(foreign_bundle)
    experiment = replace(_experiment(experiment_id="foreign-bundle-experiment"), evaluation_bundle_id=foreign_bundle.evaluation_bundle_id)
    registry.append(experiment)
    with pytest.raises(ValueError, match="EvaluationBundle dataset_snapshot_id"):
        store.complete_attempt(attempt_id=attempt.attempt_id, experiment_id=experiment.experiment_id, registry=registry)
    assert store.attempts()[0].status is TrialAttemptStatus.OPEN


def test_completion_rejects_evaluation_bundle_published_before_attempt_start(tmp_path):
    registry, _, bundle, candidate, member, _, store = _foundation(tmp_path)
    registry.append(bundle)
    attempt = store.start_attempt(semantic_attempt_id="preexisting-evaluation", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    experiment = _experiment(experiment_id="wrapped-preexisting-evaluation", outcome=ResearchOutcome.NEGATIVE)
    registry.append(experiment)
    with pytest.raises(ValueError, match="EvaluationBundle was already durable before trial attempt publication"):
        store.complete_attempt(attempt_id=attempt.attempt_id, experiment_id=experiment.experiment_id, registry=registry)
    assert store.attempts()[0].status is TrialAttemptStatus.OPEN


def test_completion_rejects_experiment_published_before_attempt_start(tmp_path):
    registry, _, _, candidate, member, _, store = _foundation(tmp_path)
    experiment = _experiment(outcome=ResearchOutcome.NEGATIVE)
    registry.append(experiment)
    attempt = store.start_attempt(semantic_attempt_id="retrospective-attempt", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    with pytest.raises(ValueError, match="already durable before trial attempt publication"):
        store.complete_attempt(attempt_id=attempt.attempt_id, experiment_id=experiment.experiment_id, registry=registry)
    assert store.attempts()[0].status is TrialAttemptStatus.OPEN


def test_same_experiment_cannot_complete_two_attempts(tmp_path):
    registry, _, bundle, candidate, member, _, store = _foundation(tmp_path)
    first = store.start_attempt(semantic_attempt_id="first", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    second = store.start_attempt(semantic_attempt_id="second", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T2)
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.NEGATIVE)
    registry.append(experiment)
    store.complete_attempt(attempt_id=first.attempt_id, experiment_id=experiment.experiment_id, registry=registry)
    with pytest.raises(ValueError, match="already consumed"):
        store.complete_attempt(attempt_id=second.attempt_id, experiment_id=experiment.experiment_id, registry=registry)


def test_sequential_truth_is_native_store_backed_and_exact_retry_is_idempotent(tmp_path):
    registry, _, bundle, candidate, member, plan, store = _foundation(tmp_path)
    attempt = store.start_attempt(semantic_attempt_id="look-attempt", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.NULL)
    registry.append(experiment)
    store.complete_attempt(attempt_id=attempt.attempt_id, experiment_id=experiment.experiment_id, registry=registry)
    first = _look(plan, member, bundle, experiment, index=1, p="0.5")
    assert store.register_sequential_look(attempt_id=attempt.attempt_id, evidence=first, registry=registry) is SequentialDecision.CONTINUE
    assert store.register_sequential_look(attempt_id=attempt.attempt_id, evidence=first, registry=registry) is SequentialDecision.CONTINUE
    assert store.snapshot(as_of=T4).registered_looks == 1
    with pytest.raises(ValueError, match="look_index"):
        store.register_sequential_look(attempt_id=attempt.attempt_id, evidence=_look(plan, member, bundle, experiment, index=3, p="0.5"), registry=registry)


def test_trial_event_cannot_backdate_durable_sequential_history(tmp_path):
    registry, _, bundle, candidate, member, plan, store = _foundation(tmp_path)
    attempt = store.start_attempt(semantic_attempt_id="look-first", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.NULL)
    registry.append(experiment)
    store.complete_attempt(attempt_id=attempt.attempt_id, experiment_id=experiment.experiment_id, registry=registry)
    look = _look(plan, member, bundle, experiment, index=1, p="0.5", observed_at=T4)
    assert store.register_sequential_look(attempt_id=attempt.attempt_id, evidence=look, registry=registry) is SequentialDecision.CONTINUE
    with pytest.raises(ValueError, match="durable sequential history"):
        store.start_attempt(semantic_attempt_id="backdated-after-look", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T3)
    assert store.snapshot(as_of=T4).total_attempts == 1


def test_sequential_append_rechecks_trial_high_water_inside_workspace_lock(tmp_path, monkeypatch):
    registry, _, bundle, candidate, member, plan, store = _foundation(tmp_path)
    attempt = store.start_attempt(semantic_attempt_id="prepared-look", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.NULL)
    registry.append(experiment)
    store.complete_attempt(attempt_id=attempt.attempt_id, experiment_id=experiment.experiment_id, registry=registry)
    stale_look = _look(plan, member, bundle, experiment, index=1, p="0.5", observed_at=T3)

    original_append = SequentialMultiplicityEvidenceStore.append
    injected = False

    def interleaving_append(sequential_store, evidence, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            store.start_attempt(
                semantic_attempt_id="interleaved-t4",
                member_authority_id=member.member_authority_id,
                candidate=candidate,
                created_at=T4,
            )
        return original_append(sequential_store, evidence, **kwargs)

    monkeypatch.setattr(SequentialMultiplicityEvidenceStore, "append", interleaving_append)
    with pytest.raises(ValueError, match="must not backdate durable family history"):
        store.register_sequential_look(attempt_id=attempt.attempt_id, evidence=stale_look, registry=registry)
    assert injected
    snapshot = store.snapshot(as_of=T4)
    assert snapshot.total_attempts == 2
    assert snapshot.registered_looks == 0


def test_promotion_guard_requires_exact_hashes_count_registry_and_native_sequential_truth(tmp_path):
    registry, _, bundle, candidate, member, plan, store = _foundation(tmp_path)
    attempt = store.start_attempt(semantic_attempt_id="positive-attempt", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.POSITIVE)
    registry.append(experiment)
    store.complete_attempt(attempt_id=attempt.attempt_id, experiment_id=experiment.experiment_id, registry=registry)
    assert store.register_sequential_look(attempt_id=attempt.attempt_id, evidence=_look(plan, member, bundle, experiment), registry=registry) is SequentialDecision.REJECT_NULL
    evidence = _promotion_evidence(store=store, experiment=experiment, bundle=bundle)
    registry.append(evidence)
    snapshot = store.assert_promotion_evidence_eligible(evidence=evidence, registry=registry, accounted_attempt_count=1)
    assert snapshot.total_attempts == snapshot.completed_attempts == snapshot.positive == 1
    with pytest.raises(ValueError, match="accounted-attempt"):
        store.assert_promotion_evidence_eligible(evidence=evidence, registry=registry, accounted_attempt_count=0)


def test_promotion_guard_rejects_positive_result_without_registered_sequential_decision(tmp_path):
    registry, _, bundle, candidate, member, _, store = _foundation(tmp_path)
    attempt = store.start_attempt(semantic_attempt_id="positive-attempt", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.POSITIVE)
    registry.append(experiment)
    store.complete_attempt(attempt_id=attempt.attempt_id, experiment_id=experiment.experiment_id, registry=registry)
    evidence = _promotion_evidence(store=store, experiment=experiment, bundle=bundle)
    registry.append(evidence)
    with pytest.raises(ValueError, match="registered sequential evidence"):
        store.assert_promotion_evidence_eligible(evidence=evidence, registry=registry, accounted_attempt_count=1)


def test_event_tamper_is_detected_before_authority_acceptance(tmp_path):
    _, _, _, candidate, member, _, store = _foundation(tmp_path)
    attempt = store.start_attempt(semantic_attempt_id="tamper-attempt", member_authority_id=member.member_authority_id, candidate=candidate, created_at=T1)
    store.abort_attempt(attempt_id=attempt.attempt_id, aborted_at=T2, reason="original reason")
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["events"][-1]["payload"]["reason"] = "rewritten reason"
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="event digest mismatch"):
        TrialFamilyAccountingStore(store.path, workspace_root=store.workspace_root, authority_root=store.authority_root)


def _runtime_registry_attack(monkeypatch, calls: list[str], *, read=True, validate=False, durable_reader=False) -> None:
    if read:
        def forged_read(_self):
            calls.append("read")
            return {"schema_version": 1, "records": []}

        monkeypatch.setattr(ScientificRegistry, "_read", forged_read)

    if validate:
        def forged_validate(_entry):
            calls.append("validate")

        monkeypatch.setattr(
            ScientificRegistry,
            "_validate_entry",
            staticmethod(forged_validate),
        )

    if durable_reader:
        def forged_reader(_path):
            calls.append("durable-reader")
            return '{"schema_version":1,"records":[]}'

        monkeypatch.setattr(
            registry_read_authority._integrity,
            "read_verified_scientific_registry_text",
            forged_reader,
        )


def test_runtime_read_replacement_cannot_authorize_attempt_start(tmp_path, monkeypatch):
    registry, _, _, candidate, member, _, store = _foundation(tmp_path)
    registry_before = registry.path.read_bytes()
    trial_before = store.path.read_bytes()
    calls: list[str] = []
    _runtime_registry_attack(monkeypatch, calls)

    with pytest.raises(RuntimeError, match="ScientificRegistry.*authority|ScientificRegistry.*changed"):
        store.start_attempt(
            semantic_attempt_id="runtime-read-attack",
            member_authority_id=member.member_authority_id,
            candidate=candidate,
            created_at=T1,
        )

    assert calls == []
    assert registry.path.read_bytes() == registry_before
    assert store.path.read_bytes() == trial_before
    assert store.attempts() == ()


def test_runtime_validator_replacement_cannot_authorize_completion(tmp_path, monkeypatch):
    registry, _, bundle, candidate, member, _, store = _foundation(tmp_path)
    attempt = store.start_attempt(
        semantic_attempt_id="validator-attack",
        member_authority_id=member.member_authority_id,
        candidate=candidate,
        created_at=T1,
    )
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.NEGATIVE)
    registry.append(experiment)

    registry_before = registry.path.read_bytes()
    trial_before = store.path.read_bytes()
    calls: list[str] = []
    _runtime_registry_attack(monkeypatch, calls, read=False, validate=True)

    with pytest.raises(RuntimeError, match="ScientificRegistry.*authority|ScientificRegistry.*changed"):
        store.complete_attempt(
            attempt_id=attempt.attempt_id,
            experiment_id=experiment.experiment_id,
            registry=registry,
        )

    assert calls == []
    assert registry.path.read_bytes() == registry_before
    assert store.path.read_bytes() == trial_before


def test_runtime_read_validator_and_reader_cosubstitution_cannot_authorize_sequential_write(
    tmp_path,
    monkeypatch,
):
    registry, _, bundle, candidate, member, plan, store = _foundation(tmp_path)
    attempt = store.start_attempt(
        semantic_attempt_id="sequential-runtime-attack",
        member_authority_id=member.member_authority_id,
        candidate=candidate,
        created_at=T1,
    )
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.NULL)
    registry.append(experiment)
    store.complete_attempt(
        attempt_id=attempt.attempt_id,
        experiment_id=experiment.experiment_id,
        registry=registry,
    )
    evidence = _look(plan, member, bundle, experiment, index=1, p="0.5")

    sequential_store = store._sequential()
    registry_before = registry.path.read_bytes()
    trial_before = store.path.read_bytes()
    sequential_before = sequential_store.path.read_bytes()
    calls: list[str] = []
    _runtime_registry_attack(
        monkeypatch,
        calls,
        read=True,
        validate=True,
        durable_reader=True,
    )

    with pytest.raises(RuntimeError, match="ScientificRegistry.*authority|ScientificRegistry.*changed"):
        store.register_sequential_look(
            attempt_id=attempt.attempt_id,
            evidence=evidence,
            registry=registry,
        )

    assert calls == []
    assert registry.path.read_bytes() == registry_before
    assert store.path.read_bytes() == trial_before
    assert sequential_store.path.read_bytes() == sequential_before


def test_runtime_durable_reader_substitution_cannot_authorize_promotion(tmp_path, monkeypatch):
    registry, _, bundle, candidate, member, plan, store = _foundation(tmp_path)
    attempt = store.start_attempt(
        semantic_attempt_id="promotion-runtime-attack",
        member_authority_id=member.member_authority_id,
        candidate=candidate,
        created_at=T1,
    )
    registry.append(bundle)
    experiment = _experiment(outcome=ResearchOutcome.POSITIVE)
    registry.append(experiment)
    store.complete_attempt(
        attempt_id=attempt.attempt_id,
        experiment_id=experiment.experiment_id,
        registry=registry,
    )
    assert store.register_sequential_look(
        attempt_id=attempt.attempt_id,
        evidence=_look(plan, member, bundle, experiment),
        registry=registry,
    ) is SequentialDecision.REJECT_NULL
    evidence = _promotion_evidence(store=store, experiment=experiment, bundle=bundle)
    registry.append(evidence)

    sequential_store = store._sequential()
    registry_before = registry.path.read_bytes()
    trial_before = store.path.read_bytes()
    sequential_before = sequential_store.path.read_bytes()
    calls: list[str] = []
    _runtime_registry_attack(
        monkeypatch,
        calls,
        read=False,
        validate=False,
        durable_reader=True,
    )

    with pytest.raises(RuntimeError, match="ScientificRegistry.*authority|ScientificRegistry.*changed"):
        store.assert_promotion_evidence_eligible(
            evidence=evidence,
            registry=registry,
            accounted_attempt_count=1,
        )

    assert calls == []
    assert registry.path.read_bytes() == registry_before
    assert store.path.read_bytes() == trial_before
    assert sequential_store.path.read_bytes() == sequential_before
