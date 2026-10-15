from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betdaq_settlement_readback import (
    BetdaqEconomicEvidence,
    BetdaqEconomicReadbackError,
    BetdaqOrderSettlementObservation,
)


def _evidence() -> BetdaqEconomicEvidence:
    return BetdaqEconomicEvidence(
        method="GetOrderDetails",
        request_identity_sha256="a" * 64,
        source_payload_sha256="b" * 64,
        observed_at="2026-09-25T00:00:00Z",
        account_context_id="betdaq-auth-context:test",
    )


def _observation(**overrides: object) -> BetdaqOrderSettlementObservation:
    values: dict[str, object] = {
        "order_id": "123",
        "market_id": "200",
        "selection_id": "300",
        "order_status_code": 4,
        "sequence_number": 81,
        "issued_at": "2026-09-22T22:00:00Z",
        "last_changed_at": "2026-09-22T23:58:01Z",
        "requested_stake": Decimal("10.00"),
        "requested_price": Decimal("2.50"),
        "total_stake": Decimal("10.00"),
        "unmatched_stake": Decimal("0"),
        "average_price": Decimal("2.45"),
        "matching_timestamp": "2026-09-22T22:00:05Z",
        "polarity_code": 1,
        "punter_reference_number": "77",
        "gross_settlement_amount": Decimal("12.34"),
        "order_commission": Decimal("0.50"),
        "market_commission": Decimal("0.25"),
        "market_settled_at": "2026-09-22T23:58:00Z",
        "currency": None,
        "denomination_proven": False,
        "scalar_economic_use_proven": False,
        "final_settlement_proven": True,
        "evidence": _evidence(),
    }
    values.update(overrides)
    return BetdaqOrderSettlementObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("requested_stake", Decimal("0"), "requested_stake must be a positive provider stake"),
        ("requested_stake", Decimal("-0.01"), "requested_stake must be a positive provider stake"),
        ("requested_price", Decimal("0"), "requested_price must be a positive provider price"),
        ("requested_price", Decimal("-1"), "requested_price must be a positive provider price"),
        ("total_stake", Decimal("-0.01"), "total_stake must be a non-negative provider economic value"),
        ("unmatched_stake", Decimal("-0.01"), "unmatched_stake must be a non-negative provider economic value"),
        ("average_price", Decimal("-0.01"), "average_price must be a non-negative provider economic value"),
    ],
)
def test_order_readback_rejects_impossible_provider_money_shape(
    field: str,
    value: Decimal,
    message: str,
) -> None:
    with pytest.raises(BetdaqEconomicReadbackError, match=message):
        _observation(**{field: value})


def test_unmatched_order_can_preserve_zero_matched_stake_and_average_price() -> None:
    value = _observation(
        order_status_code=3,
        total_stake=Decimal("0"),
        unmatched_stake=Decimal("10"),
        average_price=Decimal("0"),
        gross_settlement_amount=None,
        order_commission=None,
        market_commission=None,
        market_settled_at=None,
        final_settlement_proven=False,
    )

    assert value.total_stake == Decimal("0")
    assert value.unmatched_stake == Decimal("10")
    assert value.average_price == Decimal("0")
    assert value.final_settlement_proven is False
