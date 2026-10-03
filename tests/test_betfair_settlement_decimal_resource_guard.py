from __future__ import annotations

from decimal import Decimal

import pytest

from autosport import betfair_settlement_revisions as settlement


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1e1000000"),
        Decimal("1e-1000000"),
        Decimal("0e1000000"),
        Decimal("0e-1000000"),
        Decimal("1" * 4097),
    ],
)
def test_settlement_decimal_resource_shape_rejects_amplification_before_format(
    value: Decimal,
) -> None:
    with pytest.raises(
        settlement.BetfairSettlementRevisionError,
        match="resource limit",
    ):
        settlement._dec(value, "provider_profit")


def test_settlement_decimal_resource_guard_preserves_ordinary_exact_values() -> None:
    values = (
        Decimal("0"),
        Decimal("2.5000"),
        Decimal("-1.25"),
        Decimal("0.00000001"),
        Decimal("1000000"),
    )

    assert tuple(settlement._dec(value, "field") for value in values) == values
