from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.matchbook_offer_reconciliation import (
    MatchbookOfferReadback,
    MatchbookOfferReconciliationError,
    MatchbookOfferStatus,
    reconcile_replay,
)


T0 = datetime(2026, 9, 22, 1, 45, tzinfo=timezone.utc)
RAW_A = "a" * 64
RAW_B = "b" * 64


def _offer(
    *,
    status: MatchbookOfferStatus,
    matched: str,
    remaining: str,
    at: datetime,
    raw: str,
) -> MatchbookOfferReadback:
    return MatchbookOfferReadback(
        account_context_id="acct-terminal",
        offer_id=42,
        status=status,
        original_stake=Decimal("10"),
        matched_stake=Decimal(matched),
        remaining_stake=Decimal(remaining),
        captured_at=at,
        raw_response_sha256=raw,
    )


def test_failed_offer_cannot_later_become_fully_matched_without_explicit_correction() -> None:
    failed = _offer(
        status=MatchbookOfferStatus.FAILED,
        matched="0",
        remaining="0",
        at=T0,
        raw=RAW_A,
    )
    matched = _offer(
        status=MatchbookOfferStatus.MATCHED,
        matched="10",
        remaining="0",
        at=T0 + timedelta(seconds=1),
        raw=RAW_B,
    )

    with pytest.raises(MatchbookOfferReconciliationError, match="terminal"):
        reconcile_replay((failed, matched))


def test_fully_matched_offer_cannot_later_become_cancelled_without_explicit_correction() -> None:
    matched = _offer(
        status=MatchbookOfferStatus.MATCHED,
        matched="10",
        remaining="0",
        at=T0,
        raw=RAW_A,
    )
    cancelled = _offer(
        status=MatchbookOfferStatus.CANCELLED,
        matched="10",
        remaining="0",
        at=T0 + timedelta(seconds=1),
        raw=RAW_B,
    )

    with pytest.raises(MatchbookOfferReconciliationError, match="terminal"):
        reconcile_replay((matched, cancelled))
