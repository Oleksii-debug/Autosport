from __future__ import annotations

import hashlib
import json

import pytest

from autosport import _dataset_snapshot_lineage_publication_trust_root as lineage_trust_root
from autosport import scientific_disclosure_export as disclosure_export
from autosport.dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority,
    membership_manifest_sha256,
)
from autosport.holdout_disclosure import HoldoutDisclosureGate
from autosport.point_in_time_evidence import HoldoutConsumptionLedger
from autosport.scientific_disclosure_export import (
    ScientificDisclosureExportError,
    ScientificDisclosureExporter,
)
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
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-09-24T21:30:00+00:00"
_MEMBERS = (
    hashlib.sha256(b"event-a").hexdigest(),
    hashlib.sha256(b"event-b").hexdigest(),
)
_MANIFEST = membership_manifest_sha256(_MEMBERS)


@pytest.fixture(autouse=True)
def _product_machine_authority(tmp_path, monkeypatch):
    root = (tmp_path / "product-machine-authority").resolve(strict=False)
    monkeypatch.setattr(
        lineage_trust_root,
        "_machine_account_authority_root",
        lambda: root,
    )
    return root


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


def _promotion_evidence(bundle_sha: str) -> PromotionEvidence:
    family = "protocol-1:confirmation-trial-family"
    payload = {
        "schema_version": 1,
        "experiment_id": "experiment-1",
        "research_protocol_id": "protocol-1",
        "research_question_id": "question-1",
        "hypothesis_id": "hypothesis-1",
        "candidate_strategy_version_id": "strategy-1",
        "candidate_model_version_id": "model-1",
        "evaluation_bundle_id": "eval-1",
        "evaluation_bundle_sha256": bundle_sha,
        "dataset_snapshot_id": "dataset-1",
        "holdout_access_id": promotion_holdout_access_id(
            research_protocol_id="protocol-1",
            dataset_manifest_sha256=_MANIFEST,
            source_identity="lawful-provider:fixture",
            license_identity="license-evidence:v1",
            confirmation_trial_family_id=family,
        ),
        "confirmation_trial_family_id": family,
        "estimand": "roi",
        "direction": PromotionEvidenceDirection.LOWER_IS_BETTER.value,
        "cohort_id": "dataset-1",
        "effective_sample_size": 5,
        "minimum_effective_sample_size": 3,
        "effect_interval_low": "0.05",
        "effect_interval_high": "0.15",
        "practical_improvement": "0.1",
        "guardrails_passed": True,
        "validity": PromotionEvidenceValidity.ELIGIBLE.value,
        "holdout_consumed": False,
        "stopping_rule_sha256": hashlib.sha256(b"one final evaluation").hexdigest(),
        "multiple_comparison_control_sha256": hashlib.sha256(
            b"single frozen primary metric"
        ).hexdigest(),
        "rollback_identity": "NONE",
        "uncertainty_method": "bootstrap intervals",
        "created_at": T3,
    }
    evidence_id = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    fields = {key: value for key, value in payload.items() if key != "schema_version"}
    fields["direction"] = PromotionEvidenceDirection(payload["direction"])
    fields["validity"] = PromotionEvidenceValidity(payload["validity"])
    return PromotionEvidence(evidence_id, **fields)


def _system(tmp_path, *, with_decision: bool = True):
    workspace = tmp_path / "product-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    registry = ScientificRegistry.initialize_pristine(
        workspace / "scientific-registry.json"
    )
    question = ResearchQuestion(
        "question-1",
        "Does candidate improve the frozen holdout metric?",
        SHA_A,
        T0,
    )
    hypothesis = Hypothesis(
        "hypothesis-1",
        "question-1",
        "Candidate improves the frozen primary metric.",
        "holdout metric improves",
        "no improvement or a guardrail regresses",
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
        inclusion_criteria="predeclared events",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="retained lawful source evidence",
        causal_cutoff=T1,
        evaluation_design="sealed walk-forward holdout",
        feature_set_version="v1",
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split",),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=_promotion_rule(),
        expected_artifacts=("evaluation bundle", "decision"),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, _MANIFEST, T0)
    dataset = DatasetSnapshot(
        "dataset-1",
        _MANIFEST,
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
    experiment = ExperimentRecord(
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
        notes="frozen result",
    )
    for record in (
        question,
        hypothesis,
        protocol,
        dataset,
        features,
        model,
        strategy,
        bundle,
        experiment,
    ):
        registry.append(record)

    evidence = _promotion_evidence(bundle.bundle_sha256)
    registry.append(evidence)
    decision = PromotionDecision(
        "promotion-1",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        protocol.protocol_sha256,
        "eval-1",
        bundle.bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )
    if with_decision:
        registry.record_promotion(decision)

    authority_root = lineage_trust_root._machine_account_authority_root()
    lineage = DatasetSnapshotLineageAuthority.initialize_pristine(
        workspace / "dataset-snapshot-lineage.json",
        registry,
        authority_root=authority_root,
    )
    lineage.register(snapshot_id="dataset-1", member_sha256=_MEMBERS)
    ledger = HoldoutConsumptionLedger(
        workspace / "holdout-consumption.json",
        lineage_authority=lineage,
    )
    gate = HoldoutDisclosureGate(ledger)
    exporter = ScientificDisclosureExporter(gate)
    return registry, dataset, evidence, decision, ledger, exporter


def test_legacy_direct_reproducibility_export_is_fail_closed(tmp_path) -> None:
    registry, _, _, _, _, _ = _system(tmp_path)

    with pytest.raises(ScientificDisclosureExportError, match="direct scientific"):
        registry.export_reproducibility_bundle(
            "experiment-1",
            tmp_path / "bypass.json",
        )

    assert not (tmp_path / "bypass.json").exists()


def test_safe_export_consumes_canonical_holdout_before_writing_outcome(tmp_path) -> None:
    _, dataset, evidence, decision, ledger, exporter = _system(tmp_path)
    target = tmp_path / "outward.json"

    result = exporter.export_reproducibility_bundle(
        "experiment-1",
        target,
        disclosed_at_utc=T4,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["experiment"]["outcome"] == ResearchOutcome.POSITIVE.value
    assert result.bundle_sha256 == payload["bundle_sha256"]
    assert result.promotion_evidence_id == evidence.promotion_evidence_id
    assert result.promotion_decision_id == decision.promotion_decision_id
    assert len(ledger.records()) == 1
    assert ledger.records()[0].dataset_manifest_sha256 == dataset.manifest_sha256
    assert result.holdout_consumption_id == ledger.records()[0].consumption_id


def test_missing_promotion_decision_fails_before_consumption_or_output(tmp_path) -> None:
    _, _, _, _, ledger, exporter = _system(tmp_path, with_decision=False)
    target = tmp_path / "outward.json"

    with pytest.raises(
        ScientificDisclosureExportError,
        match="exactly one canonical PromotionDecision",
    ):
        exporter.export_reproducibility_bundle(
            "experiment-1",
            target,
            disclosed_at_utc=T4,
        )

    assert ledger.records() == ()
    assert not target.exists()


def test_publish_failure_stays_consumed_and_exact_retry_is_idempotent(
    tmp_path,
    monkeypatch,
) -> None:
    _, _, _, _, ledger, exporter = _system(tmp_path)
    target = tmp_path / "outward.json"
    original_write = disclosure_export.atomic_write_json

    def fail_write(*args, **kwargs):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(disclosure_export, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="simulated publication failure"):
        exporter.export_reproducibility_bundle(
            "experiment-1",
            target,
            disclosed_at_utc=T4,
        )

    assert len(ledger.records()) == 1
    consumed_id = ledger.records()[0].consumption_id
    assert not target.exists()

    monkeypatch.setattr(disclosure_export, "atomic_write_json", original_write)
    result = exporter.export_reproducibility_bundle(
        "experiment-1",
        target,
        disclosed_at_utc="2026-09-24T21:31:00+00:00",
    )

    assert target.exists()
    assert len(ledger.records()) == 1
    assert result.holdout_consumption_id == consumed_id
