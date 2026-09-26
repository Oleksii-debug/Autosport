from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from autosport.external_validity_baseline import (
    BaselineDefinition,
    BaselineKind,
    EvaluationContractFamily,
    FrozenBaselineProtocol,
    FrozenEvidenceScope,
    PolicyEvaluation,
    REQUIRED_BASELINE_KINDS,
    canonical_evaluation_contract,
)
from autosport.external_validity_registry import (
    ExternalValidityRegistryError,
    build_registered_external_validity_report,
    canonical_policy_evaluation_bundle_sha256,
)
from autosport.opportunity import StrategyClass
from autosport.scientific_registry import (
    DatasetSnapshot,
    EvaluationBundleRef,
    ScientificRegistry,
)


T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scope() -> FrozenEvidenceScope:
    return FrozenEvidenceScope(
        dataset_sha256=_hash("dataset"),
        dataset_cutoff=T0,
        cohort_keys=("row:a", "row:b"),
        market_evidence_sha256=_hash("market"),
        outcome_evidence_sha256=_hash("outcome"),
        cost_model_sha256=_hash("cost"),
        execution_model_sha256=_hash("execution"),
    )


def _protocol() -> FrozenBaselineProtocol:
    supported = {
        BaselineKind.NO_BET_WAIT,
        BaselineKind.MARKET_IMPLIED_DEVIG,
    }
    definitions = tuple(
        BaselineDefinition(
            kind=kind,
            baseline_id=f"baseline:{kind.value}",
            implementation_sha256=_hash("impl:" + kind.value),
            config_sha256=_hash("config:" + kind.value),
            supported=kind in supported,
            unsupported_reason=(
                None if kind in supported else f"unsupported:{kind.value}"
            ),
        )
        for kind in REQUIRED_BASELINE_KINDS
    )
    contract = canonical_evaluation_contract(
        EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
    )
    return FrozenBaselineProtocol(
        protocol_id="extval:registry-test:v1",
        frozen_at=T1,
        evidence_scope=_scope(),
        candidate_id="candidate:complex",
        candidate_artifact_sha256=_hash("candidate-artifact"),
        strategy_class=StrategyClass.PREDICTIVE_EDGE,
        evaluation_contract_family=EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
        evaluation_semantics=contract["evaluation_semantics"],
        evaluation_contract_sha256=contract["evaluation_contract_sha256"],
        primary_metric=contract["primary_metric"],
        uncertainty_method=contract["uncertainty_method"],
        baselines=definitions,
    )


def _result(
    protocol: FrozenBaselineProtocol,
    policy_id: str,
    *,
    artifact_sha256: str,
    baseline_definition_sha256: str | None = None,
    metric: str = "0.05",
    scored: int = 1,
    abstained: int = 1,
) -> PolicyEvaluation:
    provisional = PolicyEvaluation(
        policy_id=policy_id,
        policy_artifact_sha256=artifact_sha256,
        protocol_sha256=protocol.identity_sha256,
        evidence_scope_sha256=protocol.evidence_scope.identity_sha256,
        cohort_sha256=protocol.evidence_scope.cohort_sha256,
        primary_metric=protocol.primary_metric,
        evaluated_at=T2,
        metric_value=metric,
        uncertainty_low="0",
        uncertainty_high="0.1",
        observed_count=protocol.evidence_scope.sample_count,
        scored_count=scored,
        abstention_count=abstained,
        total_cost="0.01",
        evaluation_bundle_sha256=_hash("provisional:" + policy_id),
        baseline_definition_sha256=baseline_definition_sha256,
    )
    return replace(
        provisional,
        evaluation_bundle_sha256=canonical_policy_evaluation_bundle_sha256(
            provisional
        ),
    )


def _evaluations(
    protocol: FrozenBaselineProtocol,
) -> tuple[PolicyEvaluation, tuple[PolicyEvaluation, ...]]:
    candidate = _result(
        protocol,
        protocol.candidate_id,
        artifact_sha256=protocol.candidate_artifact_sha256,
        metric="0.07",
    )
    baselines: list[PolicyEvaluation] = []
    for definition in protocol.baselines:
        if not definition.supported:
            continue
        if definition.kind is BaselineKind.NO_BET_WAIT:
            metric, scored, abstained = "0", 0, 2
        else:
            metric, scored, abstained = "0.03", 2, 0
        baselines.append(
            _result(
                protocol,
                definition.baseline_id,
                artifact_sha256=definition.implementation_sha256,
                baseline_definition_sha256=definition.definition_sha256,
                metric=metric,
                scored=scored,
                abstained=abstained,
            )
        )
    return candidate, tuple(baselines)


def _append_dataset(
    registry: ScientificRegistry,
    protocol: FrozenBaselineProtocol,
    *,
    dataset_snapshot_id: str = "dataset:canonical",
    manifest_sha256: str | None = None,
    causal_cutoff: str | None = None,
) -> None:
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=dataset_snapshot_id,
            manifest_sha256=manifest_sha256 or protocol.evidence_scope.dataset_sha256,
            source_identity="test-source",
            license_identity="test-license",
            causal_cutoff=causal_cutoff or protocol.evidence_scope.dataset_cutoff,
            available_at_utc=T1,
        )
    )


def _append_bundle(
    registry: ScientificRegistry,
    evaluation: PolicyEvaluation,
    *,
    bundle_id: str,
    dataset_snapshot_id: str = "dataset:canonical",
    protocol_sha256: str | None = None,
) -> None:
    registry.append(
        EvaluationBundleRef(
            evaluation_bundle_id=bundle_id,
            bundle_sha256=evaluation.evaluation_bundle_sha256,
            evaluator_source_sha256=_hash("evaluator-source"),
            dataset_snapshot_id=dataset_snapshot_id,
            protocol_sha256=protocol_sha256 or evaluation.protocol_sha256,
            artifact_hashes=(evaluation.policy_artifact_sha256,),
            created_at=T2,
        )
    )


def _seed_happy_registry(
    tmp_path,
    protocol: FrozenBaselineProtocol,
    candidate: PolicyEvaluation,
    baselines: tuple[PolicyEvaluation, ...],
) -> tuple[ScientificRegistry, str, dict[str, str]]:
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    _append_dataset(registry, protocol)
    candidate_bundle_id = "bundle-id:candidate"
    _append_bundle(registry, candidate, bundle_id=candidate_bundle_id)
    baseline_ids: dict[str, str] = {}
    for index, baseline in enumerate(baselines):
        bundle_id = f"bundle-id:baseline:{index}"
        baseline_ids[baseline.policy_id] = bundle_id
        _append_bundle(registry, baseline, bundle_id=bundle_id)
    return registry, candidate_bundle_id, baseline_ids


def test_registered_report_accepts_durable_same_origin_bundles(tmp_path):
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)
    registry, candidate_bundle_id, baseline_ids = _seed_happy_registry(
        tmp_path, protocol, candidate, baselines
    )

    report = build_registered_external_validity_report(
        registry,
        protocol,
        candidate,
        baselines,
        candidate_evaluation_bundle_id=candidate_bundle_id,
        baseline_evaluation_bundle_ids=baseline_ids,
    )

    assert report.protocol_sha256 == protocol.identity_sha256
    assert len(report.comparisons) == len(REQUIRED_BASELINE_KINDS)
    assert any(not comparison.supported for comparison in report.comparisons)
    assert report.to_payload()["truth"]["promotion_authority"] is False


def test_unregistered_or_tampered_candidate_bundle_fails_closed(tmp_path):
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)
    registry, candidate_bundle_id, baseline_ids = _seed_happy_registry(
        tmp_path, protocol, candidate, baselines
    )

    with pytest.raises(ExternalValidityRegistryError, match="EvaluationBundle is missing"):
        build_registered_external_validity_report(
            registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id="bundle-id:missing",
            baseline_evaluation_bundle_ids=baseline_ids,
        )

    tampered = replace(candidate, evaluation_bundle_sha256=_hash("tampered"))
    with pytest.raises(ExternalValidityRegistryError, match="bundle SHA"):
        build_registered_external_validity_report(
            registry,
            protocol,
            tampered,
            baselines,
            candidate_evaluation_bundle_id=candidate_bundle_id,
            baseline_evaluation_bundle_ids=baseline_ids,
        )


def test_fresh_matching_opaque_bundle_cannot_bless_changed_metric(tmp_path):
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)
    changed = replace(candidate, metric_value="0.08")
    registry = ScientificRegistry.initialize_pristine(tmp_path / "forged-registry.json")
    _append_dataset(registry, protocol)
    _append_bundle(registry, changed, bundle_id="bundle-id:changed")

    baseline_ids: dict[str, str] = {}
    for index, baseline in enumerate(baselines):
        bundle_id = f"bundle-id:baseline:{index}"
        baseline_ids[baseline.policy_id] = bundle_id
        _append_bundle(registry, baseline, bundle_id=bundle_id)

    with pytest.raises(
        ExternalValidityRegistryError,
        match="does not commit to canonical evaluation payload",
    ):
        build_registered_external_validity_report(
            registry,
            protocol,
            changed,
            baselines,
            candidate_evaluation_bundle_id="bundle-id:changed",
            baseline_evaluation_bundle_ids=baseline_ids,
        )


def test_registry_dataset_manifest_and_cutoff_must_match_frozen_scope(tmp_path):
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)

    manifest_registry = ScientificRegistry.initialize_pristine(
        tmp_path / "manifest-registry.json"
    )
    _append_dataset(
        manifest_registry,
        protocol,
        manifest_sha256=_hash("wrong-manifest"),
    )
    _append_bundle(manifest_registry, candidate, bundle_id="bundle-id:candidate")
    with pytest.raises(ExternalValidityRegistryError, match="dataset manifest"):
        build_registered_external_validity_report(
            manifest_registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id="bundle-id:candidate",
            baseline_evaluation_bundle_ids={
                result.policy_id: f"unused:{index}"
                for index, result in enumerate(baselines)
            },
        )

    cutoff_registry = ScientificRegistry.initialize_pristine(
        tmp_path / "cutoff-registry.json"
    )
    _append_dataset(
        cutoff_registry,
        protocol,
        causal_cutoff="2026-01-01T00:00:01+00:00",
    )
    _append_bundle(cutoff_registry, candidate, bundle_id="bundle-id:candidate")
    with pytest.raises(ExternalValidityRegistryError, match="dataset cutoff"):
        build_registered_external_validity_report(
            cutoff_registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id="bundle-id:candidate",
            baseline_evaluation_bundle_ids={
                result.policy_id: f"unused:{index}"
                for index, result in enumerate(baselines)
            },
        )


def test_all_registry_bundles_must_bind_exact_frozen_protocol(tmp_path):
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)
    wrong_protocol_sha = _hash("shared-but-wrong-frozen-protocol")
    registry = ScientificRegistry.initialize_pristine(tmp_path / "wrong-protocol.json")
    _append_dataset(registry, protocol)
    _append_bundle(
        registry,
        candidate,
        bundle_id="bundle-id:candidate",
        protocol_sha256=wrong_protocol_sha,
    )
    baseline_ids: dict[str, str] = {}
    for index, baseline in enumerate(baselines):
        bundle_id = f"bundle-id:baseline:{index}"
        baseline_ids[baseline.policy_id] = bundle_id
        _append_bundle(
            registry,
            baseline,
            bundle_id=bundle_id,
            protocol_sha256=wrong_protocol_sha,
        )

    with pytest.raises(
        ExternalValidityRegistryError,
        match="protocol SHA does not match frozen protocol",
    ):
        build_registered_external_validity_report(
            registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id="bundle-id:candidate",
            baseline_evaluation_bundle_ids=baseline_ids,
        )


def test_supported_baselines_must_share_registry_dataset_and_protocol(tmp_path):
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    _append_dataset(registry, protocol, dataset_snapshot_id="dataset:canonical")
    _append_dataset(registry, protocol, dataset_snapshot_id="dataset:alternate")
    _append_bundle(
        registry,
        candidate,
        bundle_id="bundle-id:candidate",
        dataset_snapshot_id="dataset:canonical",
    )

    baseline_ids: dict[str, str] = {}
    for index, baseline in enumerate(baselines):
        bundle_id = f"bundle-id:baseline:{index}"
        baseline_ids[baseline.policy_id] = bundle_id
        _append_bundle(
            registry,
            baseline,
            bundle_id=bundle_id,
            dataset_snapshot_id=(
                "dataset:alternate" if index == 0 else "dataset:canonical"
            ),
        )

    with pytest.raises(ExternalValidityRegistryError, match="dataset snapshot differs"):
        build_registered_external_validity_report(
            registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id="bundle-id:candidate",
            baseline_evaluation_bundle_ids=baseline_ids,
        )

    protocol_registry = ScientificRegistry.initialize_pristine(
        tmp_path / "protocol-registry.json"
    )
    _append_dataset(protocol_registry, protocol)
    _append_bundle(
        protocol_registry,
        candidate,
        bundle_id="bundle-id:candidate",
    )
    protocol_baseline_ids: dict[str, str] = {}
    for index, baseline in enumerate(baselines):
        bundle_id = f"bundle-id:baseline:{index}"
        protocol_baseline_ids[baseline.policy_id] = bundle_id
        _append_bundle(
            protocol_registry,
            baseline,
            bundle_id=bundle_id,
            protocol_sha256=(
                _hash("different-research-protocol")
                if index == 0
                else protocol.identity_sha256
            ),
        )

    with pytest.raises(ExternalValidityRegistryError, match="frozen protocol"):
        build_registered_external_validity_report(
            protocol_registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id="bundle-id:candidate",
            baseline_evaluation_bundle_ids=protocol_baseline_ids,
        )


def test_unsupported_baselines_need_no_registered_bundle_and_cannot_get_one(tmp_path):
    protocol = _protocol()
    candidate, baselines = _evaluations(protocol)
    registry, candidate_bundle_id, baseline_ids = _seed_happy_registry(
        tmp_path, protocol, candidate, baselines
    )

    unsupported_id = next(
        definition.baseline_id
        for definition in protocol.baselines
        if not definition.supported
    )
    report = build_registered_external_validity_report(
        registry,
        protocol,
        candidate,
        baselines,
        candidate_evaluation_bundle_id=candidate_bundle_id,
        baseline_evaluation_bundle_ids=baseline_ids,
    )
    assert any(
        comparison.baseline_id == unsupported_id and not comparison.supported
        for comparison in report.comparisons
    )

    with pytest.raises(ExternalValidityRegistryError, match="unexpected"):
        build_registered_external_validity_report(
            registry,
            protocol,
            candidate,
            baselines,
            candidate_evaluation_bundle_id=candidate_bundle_id,
            baseline_evaluation_bundle_ids={
                **baseline_ids,
                unsupported_id: "bundle-id:fabricated-unsupported",
            },
        )
