from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.anchor_feasibility import (
    AcquisitionState,
    AnchorFeasibilityError,
    AnchorObservation,
    AnchorScope,
    FeasibilityDisposition,
    evaluate_anchor_feasibility,
)


BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)
SCOPE = AnchorScope(
    sport="football",
    league="EPL",
    market="MATCH_ODDS",
    provider="reference-provider",
    currency="EUR",
)
SHA_A = "a" * 64


def _zero_result(*, sequence: int = 1, observed_at: datetime = BASE) -> AnchorObservation:
    return AnchorObservation(
        sequence=sequence,
        acquisition_id=f"acq-{sequence}",
        scope=SCOPE,
        state=AcquisitionState.ZERO_RESULT,
        observed_at=observed_at,
        source_sha256=SHA_A,
        applicable_cost=Decimal("0"),
        capital_amount=Decimal("0"),
        capital_held_hours=Decimal("0"),
    )


def test_future_observation_cannot_enter_earlier_review() -> None:
    future_row = _zero_result(observed_at=BASE + timedelta(days=10))

    with pytest.raises(AnchorFeasibilityError, match="review_as_of|future|causal"):
        evaluate_anchor_feasibility(
            scope=SCOPE,
            window_start=BASE,
            window_end=BASE + timedelta(days=14),
            review_as_of=BASE + timedelta(days=7),
            observations=[future_row],
        )


def test_fixed_fourteen_day_horizon_cannot_be_relaxed_by_caller() -> None:
    report = evaluate_anchor_feasibility(
        scope=SCOPE,
        window_start=BASE,
        window_end=BASE + timedelta(days=1),
        review_as_of=BASE + timedelta(days=2),
        observations=[_zero_result()],
        minimum_review_days=Decimal("0"),
    )

    assert report.disposition is not FeasibilityDisposition.CONTINUE_EVIDENCE


def test_sparse_truncated_input_cannot_claim_terminal_coverage() -> None:
    report = evaluate_anchor_feasibility(
        scope=SCOPE,
        window_start=BASE,
        window_end=BASE + timedelta(days=14),
        review_as_of=BASE + timedelta(days=15),
        observations=[_zero_result(observed_at=BASE + timedelta(hours=1))],
    )

    assert report.terminal_coverage_complete is False
    assert report.disposition is not FeasibilityDisposition.CONTINUE_EVIDENCE
