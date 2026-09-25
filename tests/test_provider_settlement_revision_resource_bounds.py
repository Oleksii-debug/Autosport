from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerPositionObservation,
    BookmakerPositionState,
)
from autosport.provider_settlement_revision import (
    ProviderSettlementRevision,
    ProviderSettlementRevisionError,
)


_OBSERVED_AT = "2026-09-25T00:00:00+00:00"
_HASH = "a" * 64


def _observation(**overrides: Decimal) -> BookmakerPositionObservation:
    values: dict[str, object] = {
        "venue_id": "betfair-exchange",
        "account_id": "acct-a",
        "adapter_id": "betfair-read",
        "observation_id": "settlement-resource-bound",
        "external_position_id": "bet-resource-bound",
        "state": BookmakerPositionState.SETTLED,
        "currency": "EUR",
        "observed_at": _OBSERVED_AT,
        "source_payload_sha256": _HASH,
        "provider_amount": Decimal("10"),
        "provider_amount_semantics": "backer_stake",
        "provider_side": "BACK",
        "decimal_odds": Decimal("2"),
        "gross_return": Decimal("20"),
        "external_receipt_id": "receipt-resource-bound",
    }
    values.update(overrides)
    return BookmakerPositionObservation(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_amount", Decimal("1e1000000")),
        ("decimal_odds", Decimal("2e1000000")),
        ("gross_return", Decimal("1e-1000000")),
        # Zero must not bypass exponent-shape validation merely because its
        # canonical numeric text would otherwise collapse to the single byte 0.
        ("gross_return", Decimal("0e1000000")),
        ("provider_amount", Decimal("1" * 4097)),
    ],
)
def test_settlement_decimal_shapes_are_bounded_before_canonical_expansion(
    field: str,
    value: Decimal,
) -> None:
    observation = _observation(**{field: value})

    with pytest.raises(
        ProviderSettlementRevisionError,
        match="resource limit",
    ):
        ProviderSettlementRevision(
            settlement=observation,
            available_at=_OBSERVED_AT,
            source_ref="cleared/resource-bound",
        )


def test_normal_settlement_decimal_shapes_remain_canonical() -> None:
    revision = ProviderSettlementRevision(
        settlement=_observation(
            provider_amount=Decimal("10.000"),
            decimal_odds=Decimal("2.5000"),
            gross_return=Decimal("12.3400"),
        ),
        available_at=_OBSERVED_AT,
        source_ref="cleared/normal",
    )

    assert len(revision.revision_id) == 64
