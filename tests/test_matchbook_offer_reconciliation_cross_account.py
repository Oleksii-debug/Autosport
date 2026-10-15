from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.matchbook_offer_reconciliation import (
    MatchbookOfferReadback,
    MatchbookOfferReconciliationError,
    MatchbookOfferStatus,
    reconcile_replay,
)


T0 = datetime(2026, 9, 22, 1, 40, tzinfo=timezone.utc)
RAW_A = "a" * 64
RAW_B = "b" * 64


def _offer(account_context_id: str, *, at: datetime, raw: str) -> MatchbookOfferReadback:
    return MatchbookOfferReadback(
        account_context_id=account_context_id,
        offer_id=41,
        status=MatchbookOfferStatus.OPEN,
        original_stake=Decimal("10"),
        matched_stake=Decimal("0"),
        remaining_stake=Decimal("10"),
        captured_at=at,
        raw_response_sha256=raw,
    )


def test_replay_fails_closed_on_same_offer_id_from_different_account_contexts() -> None:
    first = _offer("account-A", at=T0, raw=RAW_A)
    second = _offer(
        "account-B",
        at=T0 + timedelta(seconds=1),
        raw=RAW_B,
    )

    with pytest.raises(
        MatchbookOfferReconciliationError,
        match="account",
    ):
        reconcile_replay((first, second))
