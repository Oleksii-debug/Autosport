from datetime import datetime, timezone
from decimal import Decimal

import pytest

import autosport.matchbook_offer_reconciliation as reconciliation
from autosport.matchbook_offer_reconciliation import (
    MatchbookOfferReadback,
    MatchbookOfferReconciliationError,
    MatchbookOfferStatus,
    MatchbookRetryDisposition,
    reconcile_replay,
    retry_disposition,
)


CAPTURED_AT = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)


def _forged_readback(
    *,
    offer_id: int,
    status: MatchbookOfferStatus,
    matched_stake: str,
    remaining_stake: str,
) -> MatchbookOfferReadback:
    return MatchbookOfferReadback(
        "acct",
        offer_id,
        status,
        Decimal("10"),
        Decimal(matched_stake),
        Decimal(remaining_stake),
        CAPTURED_AT,
        "f" * 64,
    )


def _attempt_caller_issuance(readback: MatchbookOfferReadback) -> None:
    """Exercise any caller-visible private issuer without requiring its existence."""
    issuer = getattr(reconciliation, "_issue_provider_readback", None)
    if issuer is None:
        return
    try:
        issuer(readback)
    except (MatchbookOfferReconciliationError, TypeError):
        # A guarded or no-longer-compatible issuer is an acceptable fail-closed
        # implementation. The assertions below verify that it did not mint
        # provider-origin authority as a side effect.
        return


def test_module_private_issuer_cannot_mint_retry_suppression_authority() -> None:
    forged = _forged_readback(
        offer_id=4242,
        status=MatchbookOfferStatus.OPEN,
        matched_stake="0",
        remaining_stake="10",
    )
    assert not forged.provider_origin_authoritative

    _attempt_caller_issuance(forged)

    assert not forged.provider_origin_authoritative
    assert (
        retry_disposition(
            observed_offer=forged,
            expected_account_context_id=forged.account_context_id,
            expected_offer_id=forged.offer_id,
        )
        is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
    )


def test_module_private_issuer_cannot_mint_replay_exposure_authority() -> None:
    forged = _forged_readback(
        offer_id=4343,
        status=MatchbookOfferStatus.MATCHED,
        matched_stake="10",
        remaining_stake="0",
    )
    assert not forged.provider_origin_authoritative

    _attempt_caller_issuance(forged)

    assert not forged.provider_origin_authoritative
    with pytest.raises(MatchbookOfferReconciliationError, match="provider origin"):
        reconcile_replay([forged])
