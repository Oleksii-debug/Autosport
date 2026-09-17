import json
from dataclasses import replace

import pytest

from autosport.scientific_registry import (
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
    PromotionEvidenceError,
    ResearchOutcome,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
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
    import hashlib
    canonical = json.dumps(
        record.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _binding(
    question: ResearchQuestion | None = None,
    hypothesis: Hypothesis | None = None,
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
        feature_set_version="features-v1",
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split", "source split"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule="promote only if primary improves and guardrails pass",
        expected_artifacts=("evaluation bundle", "decision"),
        code_config_sha256=SHA_C,
        frozen_at_utc=T0,
    )


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


def test_promotion_fails_closed_then_tracks_promote_and_rollback_lineage(tmp_path):
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

    promote = replace(
        missing,
        promotion_decision_id="promotion-1",
        evaluation_bundle_id="eval-1",
    )
    registry.record_promotion(promote)
    assert registry.champion_strategy(as_of=T3) == "strategy-1"

    strategy2 = StrategyVersion(
        "strategy-2",
        "canonical-strategy",
        SHA_C,
        SHA_D,
        SHA_A,
        T3,
        model_version_id="model-1",
        predecessor_strategy_version_id="strategy-1",
    )
    registry.append(strategy2)
    bundle2 = EvaluationBundleRef(
        "eval-2",
        SHA_A,
        SHA_C,
        "dataset-1",
        foundation["protocol"].protocol_sha256,
        (SHA_B,),
        T3,
    )
    registry.append(bundle2)
    experiment2 = replace(
        experiment,
        experiment_id="experiment-2",
        strategy_version_id="strategy-2",
        evaluation_bundle_id="eval-2",
        config_sha256=SHA_C,
        completed_at=T3,
    )
    registry.append(experiment2)
    promote2 = PromotionDecision(
        "promotion-2",
        PromotionAction.PROMOTE,
        "strategy-2",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-2",
        SHA_A,
        T3,
        predecessor_strategy_version_id="strategy-1",
        candidate_model_version_id="model-1",
    )
    registry.record_promotion(promote2)
    assert registry.champion_strategy(as_of=T3) == "strategy-2"

    rollback = PromotionDecision(
        "rollback-1",
        PromotionAction.ROLLBACK,
        "strategy-2",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-2",
        SHA_A,
        T3,
        predecessor_strategy_version_id="strategy-2",
        rollback_to_strategy_version_id="strategy-1",
        candidate_model_version_id="model-1",
    )
    registry.record_promotion(rollback)
    assert registry.champion_strategy(as_of=T3) == "strategy-1"


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


def test_champion_history_orders_mixed_timezone_offsets_by_instant(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific_registry.json")
    earlier = PromotionDecision(
        "promotion-offset-earlier",
        PromotionAction.PROMOTE,
        "strategy-offset-1",
        "protocol-unused",
        SHA_A,
        "eval-unused-1",
        SHA_B,
        "2026-01-04T01:30:00+02:00",
    )
    later = PromotionDecision(
        "promotion-offset-later",
        PromotionAction.PROMOTE,
        "strategy-offset-2",
        "protocol-unused",
        SHA_A,
        "eval-unused-2",
        SHA_B,
        "2026-01-04T00:00:00+00:00",
        predecessor_strategy_version_id="strategy-offset-1",
    )
    registry.append(earlier)
    registry.append(later)

    assert registry.champion_strategy(as_of="2026-01-04T01:00:00+00:00") == "strategy-offset-2"
