from decimal import Decimal

import pytest

from autosport.risk import RiskOfRuinEvidence
from autosport.risk_of_ruin_authority import (
    _product_result_matches_evidence,
    _resolve_product_evaluator_result,
)
from autosport.risk_of_ruin_evaluator import (
    IssuedRiskOfRuinResult,
    ProductRiskOfRuinEvaluator,
    RiskEvidenceClass,
    RiskOfRuinIssuanceError,
    RiskTargetKind,
)


EVALUATED_AT = "2026-09-16T15:00:01+00:00"
ISSUED_AT = "2026-09-16T15:00:02+00:00"
AVAILABLE_BY = "2026-09-16T15:00:03+00:00"
PROTOCOL_SHA = "a" * 64
BUNDLE_SHA = "b" * 64
PORTFOLIO_SHA = "c" * 64
CAPITAL_SHA = "d" * 64
CANDIDATE_SHA = "e" * 64
DATASET_MANIFEST_SHA = "f" * 64
OBSERVATION_MANIFEST_SHA = "1" * 64
EVALUATOR_SOURCE_SHA = "2" * 64
RESULT_ID = "3" * 64
REQUEST_SHA = "4" * 64


def _issued_single_result() -> IssuedRiskOfRuinResult:
    return IssuedRiskOfRuinResult(
        workspace_instance_id="workspace-1",
        result_id=RESULT_ID,
        request_sha256=REQUEST_SHA,
        target_kind=RiskTargetKind.SINGLE,
        bankroll_id="paper-bankroll",
        currency="USD",
        base_portfolio_sha256=PORTFOLIO_SHA,
        capital_state_sha256=CAPITAL_SHA,
        target_sha256=CANDIDATE_SHA,
        evaluated_stakes=(Decimal("1"),),
        research_protocol_sha256=PROTOCOL_SHA,
        reproducibility_bundle_sha256=BUNDLE_SHA,
        dataset_snapshot_id="risk-dataset",
        dataset_manifest_sha256=DATASET_MANIFEST_SHA,
        observation_manifest_sha256=OBSERVATION_MANIFEST_SHA,
        causal_cutoff="2026-09-16T14:59:58+00:00",
        evaluated_at=EVALUATED_AT,
        issued_at=ISSUED_AT,
        evidence_class=RiskEvidenceClass.PAPER,
        method_id="clopper-pearson-one-sided-fixed-n-iid-v1",
        evaluator_source_sha256=EVALUATOR_SOURCE_SHA,
        stopping_rule="fixed_n_preregistered_v1",
        independence_contract="distinct_preregistered_independent_units_v1",
        confidence_level=Decimal("0.99"),
        ruin_threshold=Decimal("0"),
        independent_units=100,
        ruin_count=0,
        upper_bound=Decimal("0.005"),
    )


def _single_evidence(result: IssuedRiskOfRuinResult) -> RiskOfRuinEvidence:
    return RiskOfRuinEvidence(
        evidence_id=result.result_id,
        research_protocol_sha256=result.research_protocol_sha256,
        reproducibility_bundle_sha256=result.reproducibility_bundle_sha256,
        producer_identity=result.producer_identity,
        causal_cutoff=result.causal_cutoff,
        evaluated_at=result.evaluated_at,
        bankroll_id=result.bankroll_id,
        currency=result.currency,
        base_portfolio_sha256=result.base_portfolio_sha256,
        candidate_sha256=result.target_sha256,
        evaluated_stake=result.evaluated_stakes[0],
        upper_bound=result.upper_bound,
    )


def test_product_evaluator_result_binding_matches_exact_policy_evidence() -> None:
    result = _issued_single_result()
    evidence = _single_evidence(result)

    assert _product_result_matches_evidence(
        result,
        evidence,
        kind="single",
        available_by=AVAILABLE_BY,
        evaluation_available_at=ISSUED_AT,
        dataset_snapshot_id=result.dataset_snapshot_id,
        dataset_manifest_sha256=result.dataset_manifest_sha256,
        evaluator_source_sha256=result.evaluator_source_sha256,
        effective_sample_size=result.independent_units,
    )

    tampered = RiskOfRuinEvidence(
        evidence_id=evidence.evidence_id,
        research_protocol_sha256=evidence.research_protocol_sha256,
        reproducibility_bundle_sha256=evidence.reproducibility_bundle_sha256,
        producer_identity=evidence.producer_identity,
        causal_cutoff=evidence.causal_cutoff,
        evaluated_at=evidence.evaluated_at,
        bankroll_id=evidence.bankroll_id,
        currency=evidence.currency,
        base_portfolio_sha256=evidence.base_portfolio_sha256,
        candidate_sha256=evidence.candidate_sha256,
        evaluated_stake=evidence.evaluated_stake,
        upper_bound=Decimal("0.004"),
    )
    assert not _product_result_matches_evidence(
        result,
        tampered,
        kind="single",
        available_by=AVAILABLE_BY,
        evaluation_available_at=ISSUED_AT,
        dataset_snapshot_id=result.dataset_snapshot_id,
        dataset_manifest_sha256=result.dataset_manifest_sha256,
        evaluator_source_sha256=result.evaluator_source_sha256,
        effective_sample_size=result.independent_units,
    )


def test_product_evaluator_dispatch_rebinding_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        ProductRiskOfRuinEvaluator,
        "resolve",
        lambda self, result_id: _issued_single_result(),
    )

    with pytest.raises(RiskOfRuinIssuanceError, match="executable authority was rebound"):
        _resolve_product_evaluator_result(tmp_path, RESULT_ID)
