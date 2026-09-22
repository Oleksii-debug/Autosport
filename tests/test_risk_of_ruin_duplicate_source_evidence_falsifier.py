from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.risk_of_ruin_evaluator import (
    RiskEvidenceClass,
    RiskOfRuinEvaluationError,
    RiskOfRuinEvaluationRequest,
    RiskPathObservation,
    RiskTargetKind,
    clopper_pearson_upper_bound,
)


SAME_SOURCE_EVIDENCE_SHA256 = "a" * 64
PORTFOLIO_SHA256 = "b" * 64
CAPITAL_STATE_SHA256 = "c" * 64
TARGET_SHA256 = "d" * 64
PROTOCOL_SHA256 = "e" * 64
BUNDLE_SHA256 = "f" * 64
DATASET_MANIFEST_SHA256 = "1" * 64


def _duplicated_source_observations(count: int) -> tuple[RiskPathObservation, ...]:
    return tuple(
        RiskPathObservation(
            independent_unit_id=f"unit-{index:04d}",
            dependence_group_id=f"group-{index:04d}",
            minimum_equity=Decimal("100"),
            outcome_available_at="2026-01-01T00:00:00+00:00",
            source_evidence_sha256=SAME_SOURCE_EVIDENCE_SHA256,
        )
        for index in range(count)
    )


def test_duplicate_source_evidence_cannot_inflate_fixed_n_sample() -> None:
    """One evidence identity cannot become N independent trials by relabeling it."""

    one_observation_upper = clopper_pearson_upper_bound(
        ruin_count=0,
        independent_units=1,
        confidence_level=Decimal("0.95"),
    )
    inflated_upper = clopper_pearson_upper_bound(
        ruin_count=0,
        independent_units=100,
        confidence_level=Decimal("0.95"),
    )

    assert one_observation_upper == Decimal("0.95")
    assert inflated_upper < Decimal("0.03")

    observations = _duplicated_source_observations(100)

    with pytest.raises(
        RiskOfRuinEvaluationError,
        match="source.*evidence|evidence.*duplicate|unique.*evidence",
    ):
        RiskOfRuinEvaluationRequest(
            target_kind=RiskTargetKind.SINGLE,
            bankroll_id="bankroll:test",
            currency="EUR",
            base_portfolio_sha256=PORTFOLIO_SHA256,
            capital_state_sha256=CAPITAL_STATE_SHA256,
            target_sha256=TARGET_SHA256,
            evaluated_stakes=(Decimal("1"),),
            research_protocol_sha256=PROTOCOL_SHA256,
            reproducibility_bundle_sha256=BUNDLE_SHA256,
            dataset_snapshot_id="dataset:test:001",
            dataset_manifest_sha256=DATASET_MANIFEST_SHA256,
            causal_cutoff="2026-01-02T00:00:00+00:00",
            evaluated_at="2026-01-03T00:00:00+00:00",
            confidence_level=Decimal("0.95"),
            ruin_threshold=Decimal("0"),
            planned_independent_units=100,
            evidence_class=RiskEvidenceClass.PAPER,
            observations=observations,
        )
