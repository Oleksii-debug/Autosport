from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

import autosport.forward_economic_evidence as evidence
from autosport.forward_economic_evidence import (
    AlphaAllocation,
    FamilywiseAlphaRegistry,
    ForwardEconomicEvidenceError,
)


T0 = datetime(2026, 9, 23, tzinfo=timezone.utc)


def test_familywise_alpha_extreme_scale_fails_before_fraction_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical alpha inputs must be resource-bounded before Fraction expansion."""

    real_fraction = evidence.Fraction
    oversized_fraction_seen = False

    def guarded_fraction(value: object):
        nonlocal oversized_fraction_seen
        if type(value) is Decimal:
            exponent = value.as_tuple().exponent
            if type(exponent) is int and abs(exponent) > 512:
                oversized_fraction_seen = True
                raise RuntimeError("oversized Decimal reached Fraction materialization")
        return real_fraction(value)

    monkeypatch.setattr(evidence, "Fraction", guarded_fraction)

    with pytest.raises(ForwardEconomicEvidenceError):
        allocation = AlphaAllocation(
            challenger_id="challenger-a",
            alpha=Decimal("1E-6000"),
        )
        FamilywiseAlphaRegistry(
            family_id="family-a",
            total_alpha=Decimal("0.5"),
            allocations=(allocation,),
            sealed_at=T0,
        )

    assert oversized_fraction_seen is False


def test_familywise_alpha_normal_exact_values_remain_supported() -> None:
    registry = FamilywiseAlphaRegistry(
        family_id="family-a",
        total_alpha=Decimal("0.7"),
        allocations=(
            AlphaAllocation(challenger_id="a", alpha=Decimal("0.35")),
            AlphaAllocation(challenger_id="b", alpha=Decimal("0.35")),
        ),
        sealed_at=T0,
    )

    assert registry.allocation_for("a") == Decimal("0.35")
    assert registry.allocation_for("b") == Decimal("0.35")


def test_familywise_total_alpha_extreme_scale_fails_before_fraction_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """total_alpha must use the same bounded representation before Fraction."""

    real_fraction = evidence.Fraction
    oversized_fraction_seen = False

    def guarded_fraction(value: object):
        nonlocal oversized_fraction_seen
        if type(value) is Decimal:
            exponent = value.as_tuple().exponent
            if type(exponent) is int and abs(exponent) > 512:
                oversized_fraction_seen = True
                raise RuntimeError("oversized Decimal reached Fraction materialization")
        return real_fraction(value)

    monkeypatch.setattr(evidence, "Fraction", guarded_fraction)

    allocation = AlphaAllocation(
        challenger_id="challenger-a",
        alpha=Decimal("0.1"),
    )
    with pytest.raises(ForwardEconomicEvidenceError):
        FamilywiseAlphaRegistry(
            family_id="family-a",
            total_alpha=Decimal("1E-6000"),
            allocations=(allocation,),
            sealed_at=T0,
        )

    assert oversized_fraction_seen is False
