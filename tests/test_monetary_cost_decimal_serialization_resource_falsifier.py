from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.monetary_cost_authority import (
    MonetaryAuthorityError,
    MonetaryEvidenceQuality,
    MonetarySourceSnapshot,
)


T0 = datetime(2026, 9, 22, 7, 58, tzinfo=timezone.utc)


def _snapshot(amount: Decimal) -> MonetarySourceSnapshot:
    return MonetarySourceSnapshot(
        authority_id="decimal-serialization-resource-test",
        evidence_id="compact-extreme-decimal",
        content_sha256="a" * 64,
        amount=amount,
        currency="EUR",
        campaign_ids=("campaign-a",),
        coverage_start=T0,
        coverage_end=T0,
        observed_at=T0,
        available_at=T0,
        provenance="test:compact-extreme-decimal",
        quality=MonetaryEvidenceQuality.ESTIMATE,
    )


@pytest.mark.parametrize(
    "amount",
    (
        Decimal("1E+100000"),
        Decimal("1E-100000"),
    ),
)
def test_compact_extreme_money_cannot_expand_canonical_serialization(
    amount: Decimal,
) -> None:
    """Compact Decimal input must stay bounded or fail closed before expansion."""

    try:
        snapshot = _snapshot(amount)
        encoded = snapshot.payload()["amount"]
    except MonetaryAuthorityError:
        # A deliberate product-owned Decimal range/encoding bound is acceptable.
        return

    assert isinstance(encoded, str)
    assert len(encoded) <= 1024, (
        "canonical money serialization expanded a compact Decimal in proportion "
        "to its exponent distance"
    )
