from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.matchbook_offer_reconciliation import (
    MatchbookOfferReadback,
    MatchbookOfferStatus,
    MatchbookRetryDisposition,
    retry_disposition,
)


@pytest.mark.parametrize(
    "status",
    (MatchbookOfferStatus.OPEN, MatchbookOfferStatus.DELAYED),
)
def test_caller_constructed_readback_cannot_suppress_retry_without_origin_authority(
    status: MatchbookOfferStatus,
) -> None:
    """A public DTO plus an arbitrary digest is not authenticated provider evidence."""

    forged = MatchbookOfferReadback(
        account_context_id="acct",
        offer_id=4242,
        status=status,
        original_stake=Decimal("10"),
        matched_stake=Decimal("0"),
        remaining_stake=Decimal("10"),
        captured_at=datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc),
        raw_response_sha256="f" * 64,
    )

    assert forged.provider_write_authority is False
    assert forged.settlement_authority is False
    assert (
        retry_disposition(
            observed_offer=forged,
            expected_account_context_id="acct",
            expected_offer_id=4242,
        )
        is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
    )
