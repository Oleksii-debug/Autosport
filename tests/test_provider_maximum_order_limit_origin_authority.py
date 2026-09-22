from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.provider_maximum_order_limit import (
    ProviderMaximumOrderLimitError,
    ProviderMaximumOrderLimitEvidence,
    ProviderMaximumOrderLimitKind,
    ProviderMaximumOrderLimitSourceKind,
    assess_provider_maximum_order_limit,
    verify_provider_maximum_order_limit_evidence,
)


def test_caller_fabricated_maximum_cannot_mint_verified_provider_limit_truth() -> None:
    """A local DTO + matching fake digests are not authenticated provider evidence."""

    now = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
    fabricated_source = "a" * 64
    fabricated_action = "b" * 64
    evidence = ProviderMaximumOrderLimitEvidence(
        provider_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-api-v1",
        jurisdiction="INTL",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        order_family="LIMIT",
        currency="EUR",
        limit_kind=ProviderMaximumOrderLimitKind.BACK_STAKE_PER_ORDER,
        maximum_amount=Decimal("999999"),
        observed_at=now - timedelta(seconds=1),
        valid_until=now + timedelta(minutes=1),
        source_kind=ProviderMaximumOrderLimitSourceKind.AUTHENTICATED_ACTION_QUOTE,
        source_ref="caller-invented-provider-response",
        source_payload_sha256=fabricated_source,
        action_binding_sha256=fabricated_action,
    )

    with pytest.raises((ProviderMaximumOrderLimitError, TypeError, ValueError)):
        verified = verify_provider_maximum_order_limit_evidence(
            evidence,
            as_of=now,
            provider_id="betfair",
            account_id="acct-1",
            adapter_id="betfair-api-v1",
            jurisdiction="INTL",
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            side="BACK",
            order_family="LIMIT",
            currency="EUR",
            limit_kind=ProviderMaximumOrderLimitKind.BACK_STAKE_PER_ORDER,
            action_binding_sha256=fabricated_action,
        )
        assess_provider_maximum_order_limit(
            evidence=verified,
            requested_amount=Decimal("500000"),
        )
