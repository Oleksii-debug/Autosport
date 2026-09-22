from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from autosport.risk_of_ruin_evaluator import (
    ProductRiskOfRuinEvaluator,
    RiskEvidenceClass,
    RiskOfRuinEvaluationError,
    RiskOfRuinEvaluationRequest,
    RiskOfRuinIssuanceError,
    RiskPathObservation,
    RiskTargetKind,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _evaluator(tmp_path: Path) -> ProductRiskOfRuinEvaluator:
    return ProductRiskOfRuinEvaluator(
        workspace=(tmp_path / "workspace").resolve(),
        authority_root=(tmp_path / "authority").resolve(),
    )


def _observation(
    index: int,
    *,
    source_evidence_sha256: str,
) -> RiskPathObservation:
    return RiskPathObservation(
        independent_unit_id=f"unit-{index:04d}",
        dependence_group_id=f"caller-asserted-group-{index:04d}",
        minimum_equity=Decimal("100"),
        outcome_available_at="2026-01-01T00:00:00+00:00",
        source_evidence_sha256=source_evidence_sha256,
    )


def _request(
    observations: tuple[RiskPathObservation, ...],
) -> RiskOfRuinEvaluationRequest:
    return RiskOfRuinEvaluationRequest(
        target_kind=RiskTargetKind.SINGLE,
        bankroll_id="bankroll:caller-forged",
        currency="EUR",
        base_portfolio_sha256=SHA_A,
        capital_state_sha256=SHA_B,
        target_sha256=SHA_C,
        evaluated_stakes=(Decimal("1"),),
        research_protocol_sha256=SHA_D,
        reproducibility_bundle_sha256=SHA_E,
        dataset_snapshot_id="dataset:caller-forged",
        dataset_manifest_sha256=SHA_F,
        causal_cutoff="2026-01-02T00:00:00+00:00",
        evaluated_at="2026-01-03T00:00:00+00:00",
        confidence_level=Decimal("0.95"),
        ruin_threshold=Decimal("0"),
        planned_independent_units=len(observations),
        evidence_class=RiskEvidenceClass.PAPER,
        observations=observations,
    )


def _assert_caller_only_request_cannot_become_positive_authority(
    evaluator: ProductRiskOfRuinEvaluator,
    request: RiskOfRuinEvaluationRequest,
) -> None:
    """Accept either fail-closed issuance or an explicitly non-authoritative result."""

    try:
        issued = evaluator.issue(request)
    except (RiskOfRuinEvaluationError, RiskOfRuinIssuanceError):
        return

    assert evaluator.verify(issued) is False, (
        "caller-constructed observations and caller-supplied scientific/data hashes "
        "must not become product-issued positive risk authority without canonical "
        "product re-resolution"
    )


def test_caller_constructed_paper_observations_cannot_mint_product_issued_low_risk(
    tmp_path: Path,
) -> None:
    observations = tuple(
        _observation(
            index,
            source_evidence_sha256=f"{index % 10}" * 64,
        )
        for index in range(100)
    )
    evaluator = _evaluator(tmp_path)

    _assert_caller_only_request_cannot_become_positive_authority(
        evaluator,
        _request(observations),
    )


def test_caller_group_labels_cannot_replay_one_source_as_100_independent_units(
    tmp_path: Path,
) -> None:
    observations = tuple(
        _observation(index, source_evidence_sha256=SHA_A)
        for index in range(100)
    )
    evaluator = _evaluator(tmp_path)
    request = _request(observations)

    try:
        issued = evaluator.issue(request)
    except (RiskOfRuinEvaluationError, RiskOfRuinIssuanceError):
        return

    assert issued.independent_units < 100 or evaluator.verify(issued) is False, (
        "one repeated source-evidence identity cannot become 100 statistically "
        "independent units merely because caller-supplied unit/group labels differ"
    )


def test_control_request_shape_is_not_rejected_by_local_dataclass_validation() -> None:
    observations = tuple(
        _observation(index, source_evidence_sha256=f"{index % 10}" * 64)
        for index in range(3)
    )

    request = _request(observations)

    assert request.planned_independent_units == 3
    assert request.evidence_class is RiskEvidenceClass.PAPER
