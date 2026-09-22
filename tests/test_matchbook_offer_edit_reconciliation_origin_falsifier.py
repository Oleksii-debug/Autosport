from datetime import datetime, timezone
from decimal import Decimal

from autosport.matchbook_offer_edit_reconciliation import (
    MatchbookOfferEditReadback,
    MatchbookOfferEditRetryDisposition,
    MatchbookOfferEditStatus,
    retry_disposition,
)


def _caller_readback(status: MatchbookOfferEditStatus) -> MatchbookOfferEditReadback:
    return MatchbookOfferEditReadback(
        account_context_id="acct-caller-origin",
        offer_id=101,
        offer_edit_id=9001,
        status=status,
        captured_at=datetime(2026, 9, 22, 6, 20, tzinfo=timezone.utc),
        raw_response_sha256="a" * 64,
        delay_seconds=Decimal("5")
        if status is MatchbookOfferEditStatus.DELAYED
        else None,
    )


def test_caller_constructed_delayed_edit_cannot_mint_wait_authority() -> None:
    detached = _caller_readback(MatchbookOfferEditStatus.DELAYED)

    assert (
        retry_disposition(readback=detached)
        is MatchbookOfferEditRetryDisposition.RECONCILE_BEFORE_ANY_NEW_EDIT
    )


def test_caller_constructed_applied_edit_cannot_mint_repeat_suppression() -> None:
    detached = _caller_readback(MatchbookOfferEditStatus.APPLIED)

    assert (
        retry_disposition(readback=detached)
        is MatchbookOfferEditRetryDisposition.RECONCILE_BEFORE_ANY_NEW_EDIT
    )
