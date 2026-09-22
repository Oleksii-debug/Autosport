from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.real_execution_ledger import ExecutionAction
from autosport.smarkets_execution_reconciliation import (
    SmarketsExecutionAuthority,
    SmarketsOrderReadback,
    SmarketsOrderState,
    SmarketsReconciliationError,
    verify_smarkets_order_readback,
)


def test_caller_constructed_approval_and_readback_cannot_mint_accepted_truth() -> None:
    """Matching caller DTOs/digests are not authenticated Smarkets evidence."""

    with pytest.raises((SmarketsReconciliationError, TypeError, ValueError)):
        action = ExecutionAction(
            action_id="action-caller-mint",
            bookmaker_id="smarkets",
            account_id="acct-1",
            event_id="event-7",
            market_id="market-9",
            selection_id="contract-11",
            side="BACK",
            requested_odds=Decimal("2.50"),
            requested_stake=Decimal("10.00"),
            quote_id="quote-1",
            quote_observed_at="2026-09-22T12:10:00+00:00",
            expires_at="2026-09-22T12:20:00+00:00",
        )
        profile = BookmakerCapabilityProfile(
            venue_id="smarkets",
            account_id="acct-1",
            adapter_id="smarkets-official-api",
            adapter_version="1",
            profile_version=1,
            facts=(
                BookmakerCapabilityFact(
                    BookmakerCapability.PLACE_BET,
                    BookmakerCapabilityState.SUPPORTED,
                ),
                BookmakerCapabilityFact(
                    BookmakerCapability.BET_READBACK,
                    BookmakerCapabilityState.SUPPORTED,
                ),
            ),
            observed_at="2026-09-22T12:00:00+00:00",
            source_ref="caller-invented-capability-evidence",
            source_payload_sha256="a" * 64,
        )
        authority = SmarketsExecutionAuthority(
            account_id="acct-1",
            approval_id="caller-invented-approval",
            approved_event_ids=("event-7",),
            approved_market_ids=("market-9",),
            observed_at="2026-09-22T12:00:00+00:00",
            expires_at="2026-09-22T13:00:00+00:00",
            source_ref="caller-invented-approval-source",
            source_payload_sha256="b" * 64,
            execution_approved=True,
        )
        readback = SmarketsOrderReadback(
            provider_order_id="order-100",
            account_id="acct-1",
            event_id="event-7",
            market_id="market-9",
            contract_id="contract-11",
            side="BACK",
            requested_price=Decimal("2.50"),
            requested_quantity=Decimal("10.00"),
            matched_quantity=Decimal("10.00"),
            average_matched_price=Decimal("2.48"),
            state=SmarketsOrderState.FILLED,
            observed_at="2026-09-22T12:15:00+00:00",
            source_payload_sha256="c" * 64,
        )

        verify_smarkets_order_readback(
            action,
            profile,
            authority,
            readback,
            expected_provider_order_id="order-100",
        )
