from __future__ import annotations

import hashlib

from autosport.external_validity_baseline import (
    BaselineDefinition,
    BaselineKind,
    EvaluationContractFamily,
    FrozenBaselineProtocol,
    FrozenEvidenceScope,
    PolicyEvaluation,
    REQUIRED_BASELINE_KINDS,
    build_external_validity_report,
    canonical_evaluation_contract,
)
from autosport.opportunity import StrategyClass


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _protocol() -> FrozenBaselineProtocol:
    scope = FrozenEvidenceScope(
        dataset_sha256=_sha("dataset"),
        dataset_cutoff="2026-09-22T04:00:00+00:00",
        cohort_keys=("row-a", "row-b", "row-c"),
        market_evidence_sha256=_sha("market"),
        outcome_evidence_sha256=_sha("outcome"),
        cost_model_sha256=_sha("cost-model-with-shared-costs"),
        execution_model_sha256=_sha("paper-execution"),
    )
    baselines = tuple(
        BaselineDefinition(
            kind=kind,
            baseline_id=f"baseline:{kind.value}",
            implementation_sha256=_sha(f"implementation:{kind.value}"),
            config_sha256=_sha(f"config:{kind.value}"),
            supported=kind is BaselineKind.NO_BET_WAIT,
            unsupported_reason=(
                None
                if kind is BaselineKind.NO_BET_WAIT
                else "not needed by this focused NO_ACTION cost falsifier"
            ),
        )
        for kind in REQUIRED_BASELINE_KINDS
    )
    family = EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE
    contract = canonical_evaluation_contract(family)
    return FrozenBaselineProtocol(
        protocol_id="no-action-shared-cost-falsifier-v1",
        frozen_at="2026-09-22T04:05:00+00:00",
        evidence_scope=scope,
        candidate_id="candidate",
        candidate_artifact_sha256=_sha("candidate"),
        strategy_class=StrategyClass.PREDICTIVE_EDGE,
        evaluation_contract_family=family,
        evaluation_semantics=contract["evaluation_semantics"],
        evaluation_contract_sha256=contract["evaluation_contract_sha256"],
        primary_metric=contract["primary_metric"],
        uncertainty_method=contract["uncertainty_method"],
        baselines=baselines,
    )


def _evaluation(
    protocol: FrozenBaselineProtocol,
    *,
    policy_id: str,
    artifact_sha256: str,
    metric: str,
    low: str,
    high: str,
    scored: int,
    abstained: int,
    total_cost: str,
    baseline_definition_sha256: str | None = None,
) -> PolicyEvaluation:
    scope = protocol.evidence_scope
    return PolicyEvaluation(
        policy_id=policy_id,
        policy_artifact_sha256=artifact_sha256,
        protocol_sha256=protocol.identity_sha256,
        evidence_scope_sha256=scope.identity_sha256,
        cohort_sha256=scope.cohort_sha256,
        primary_metric=protocol.primary_metric,
        evaluated_at="2026-09-22T04:10:00+00:00",
        metric_value=metric,
        uncertainty_low=low,
        uncertainty_high=high,
        observed_count=scope.sample_count,
        scored_count=scored,
        abstention_count=abstained,
        total_cost=total_cost,
        evaluation_bundle_sha256=_sha(f"bundle:{policy_id}:{total_cost}"),
        baseline_definition_sha256=baseline_definition_sha256,
    )


def test_no_action_zero_execution_does_not_force_shared_applicable_cost_to_zero() -> None:
    """Zero position/action is distinct from zero total economic cost.

    This is deliberately a comparison-semantics falsifier only. It does not
    establish product issuance/provenance for PolicyEvaluation; #1034/#762 owns
    that separate authority.
    """

    protocol = _protocol()
    no_action = next(
        item for item in protocol.baselines if item.kind is BaselineKind.NO_BET_WAIT
    )
    candidate = _evaluation(
        protocol,
        policy_id=protocol.candidate_id,
        artifact_sha256=protocol.candidate_artifact_sha256,
        metric="0.08",
        low="0.04",
        high="0.12",
        scored=2,
        abstained=1,
        total_cost="20",
    )
    control = _evaluation(
        protocol,
        policy_id=no_action.baseline_id,
        artifact_sha256=no_action.implementation_sha256,
        baseline_definition_sha256=no_action.definition_sha256,
        metric="0",
        low="0",
        high="0",
        scored=0,
        abstained=protocol.evidence_scope.sample_count,
        total_cost="5",
    )

    report = build_external_validity_report(protocol, candidate, (control,))
    comparison = next(
        item for item in report.comparisons if item.kind is BaselineKind.NO_BET_WAIT
    )

    assert comparison.baseline_total_cost == "5"
    assert comparison.candidate_total_cost == "20"
    assert comparison.total_cost_delta == "15"
    assert comparison.baseline_metric_value == "0"
    assert comparison.baseline_abstention_count == protocol.evidence_scope.sample_count
