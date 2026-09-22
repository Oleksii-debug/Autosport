from datetime import datetime, tzinfo
from decimal import Decimal

import pytest

from autosport.matchbook_offer_edit_reconciliation import (
    MatchbookOfferEditIntent,
    MatchbookOfferEditReadback,
    MatchbookOfferEditReconciliationError,
    MatchbookOfferEditStatus,
)


class _IndeterminateOffset(tzinfo):
    """tzinfo-shaped object that does not define an actual UTC offset."""

    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "INDETERMINATE"


def _ambiguous_local_datetime() -> datetime:
    return datetime(2026, 9, 22, 3, 45, tzinfo=_IndeterminateOffset())


def test_intent_rejects_tzinfo_without_concrete_utc_offset():
    with pytest.raises(
        MatchbookOfferEditReconciliationError,
        match="requested_at must be a timezone-aware datetime",
    ):
        MatchbookOfferEditIntent(
            account_context_id="acct",
            offer_id=101,
            current_odds=Decimal("2.00"),
            current_stake=Decimal("50.00"),
            new_odds=Decimal("2.10"),
            new_stake=Decimal("45.00"),
            requested_at=_ambiguous_local_datetime(),
        )


def test_readback_rejects_tzinfo_without_concrete_utc_offset():
    with pytest.raises(
        MatchbookOfferEditReconciliationError,
        match="captured_at must be a timezone-aware datetime",
    ):
        MatchbookOfferEditReadback(
            account_context_id="acct",
            offer_id=101,
            offer_edit_id=9001,
            status=MatchbookOfferEditStatus.DELAYED,
            captured_at=_ambiguous_local_datetime(),
            raw_response_sha256="a" * 64,
            delay_seconds=Decimal("5"),
        )
