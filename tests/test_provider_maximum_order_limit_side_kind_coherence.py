from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.provider_maximum_order_limit import (
    ProviderMaximumOrderLimitError,
    ProviderMaximumOrderLimitEvidence,
    ProviderMaximumOrderLimitKind,
    ProviderMaximumOrderLimitSourceKind,
)


NOW = datetime(2026, 9, 23, 20, 30, tzinfo=timezone.utc)
SHA = "a" * 64


def _evidence(
    *,
    side: str,
    limit_kind: ProviderMaximumOrderLimitKind,
) -> ProviderMaximumOrderLimitEvidence:
    return ProviderMaximumOrderLimitEvidence(
        provider_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-api-v1",
        jurisdiction="INTL",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side=side,
        order_family="LIMIT",
        currency="EUR",
        limit_kind=limit_kind,
        maximum_amount=Decimal("50"),
        observed_at=NOW,
        valid_until=NOW + timedelta(minutes=1),
        source_kind=ProviderMaximumOrderLimitSourceKind.AUTHENTICATED_PROVIDER_READBACK,
        source_ref="provider-limit-response-1",
        source_payload_sha256=SHA,
    )


@pytest.mark.parametrize(
    ("side", "limit_kind"),
    [
        ("BACK", ProviderMaximumOrderLimitKind.LAY_STAKE_PER_ORDER),
        ("BACK", ProviderMaximumOrderLimitKind.LAY_LIABILITY_PER_ORDER),
        ("LAY", ProviderMaximumOrderLimitKind.BACK_STAKE_PER_ORDER),
    ],
)
def test_side_specific_limit_kind_mismatch_fails_closed(
    side: str,
    limit_kind: ProviderMaximumOrderLimitKind,
) -> None:
    with pytest.raises(ProviderMaximumOrderLimitError):
        _evidence(side=side, limit_kind=limit_kind)


@pytest.mark.parametrize("side", ["BACK", "LAY"])
def test_order_notional_limit_remains_side_generic(side: str) -> None:
    item = _evidence(
        side=side,
        limit_kind=ProviderMaximumOrderLimitKind.ORDER_NOTIONAL_PER_ORDER,
    )

    assert item.side == side
    assert item.limit_kind is ProviderMaximumOrderLimitKind.ORDER_NOTIONAL_PER_ORDER
