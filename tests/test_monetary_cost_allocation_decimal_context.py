from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, localcontext

import pytest

from autosport.campaign_cost_evidence import CostSourceRef
from autosport.monetary_cost_authority import (
    MonetaryAuthorityError,
    SharedAllocationSnapshot,
)


T0 = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)
_SOURCE_REF = CostSourceRef(
    family="economics.monetary-source.v2:FIXED_ADMIN:allocation-test",
    evidence_id="source-allocation-decimal-context",
    sha256="a" * 64,
)


def _allocation(
    shares: tuple[tuple[str, Decimal], ...],
    *,
    evidence_id: str = "allocation-decimal-context",
) -> SharedAllocationSnapshot:
    return SharedAllocationSnapshot(
        authority_id="allocation-decimal-context-authority",
        evidence_id=evidence_id,
        content_sha256="b" * 64,
        source_ref=_SOURCE_REF,
        shares=shares,
        observed_at=T0,
        available_at=T0,
        provenance="test:exact-allocation-conservation",
    )


@pytest.mark.parametrize(
    "share",
    (
        Decimal("0.50000000000000000000000000006"),
        Decimal("0.49999999999999999999999999996"),
    ),
)
def test_rounded_to_one_invalid_allocations_fail_closed(share: Decimal) -> None:
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        with pytest.raises(MonetaryAuthorityError, match="conserve exactly one"):
            _allocation(
                (
                    ("campaign-a", share),
                    ("campaign-b", share),
                )
            )


def test_exact_long_allocation_is_context_independent_and_restart_safe() -> None:
    shares = (
        ("campaign-a", Decimal("0.50000000000000000000000000006")),
        ("campaign-b", Decimal("0.49999999999999999999999999994")),
    )

    with localcontext() as context:
        context.prec = 5
        context.rounding = ROUND_DOWN
        low = _allocation(shares, evidence_id="allocation-low-context")
        durable = low.to_dict()
        low_sha = low.sha256

    with localcontext() as context:
        context.prec = 90
        context.rounding = ROUND_UP
        high = _allocation(shares, evidence_id="allocation-low-context")
        restored = SharedAllocationSnapshot.from_dict(durable)
        high_sha = high.sha256

    assert low == high
    assert restored == low
    assert low_sha == high_sha == restored.sha256


def test_simple_existing_allocation_semantics_remain_unchanged() -> None:
    allocation = _allocation(
        (
            ("campaign-a", Decimal("0.25")),
            ("campaign-b", Decimal("0.75")),
        )
    )

    assert allocation.shares == (
        ("campaign-a", Decimal("0.25")),
        ("campaign-b", Decimal("0.75")),
    )


@pytest.mark.parametrize(
    "shares",
    (
        (
            ("campaign-a", Decimal("1")),
            ("campaign-b", Decimal("1E-1000000000")),
        ),
        (("campaign-a", Decimal("1E+1000000000")),),
    ),
)
def test_extreme_exponent_gap_rejects_without_gap_materialization(
    shares: tuple[tuple[str, Decimal], ...],
) -> None:
    with pytest.raises(MonetaryAuthorityError, match="conserve exactly one"):
        _allocation(shares)

