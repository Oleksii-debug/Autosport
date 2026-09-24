from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import pickle

import pytest

import autosport.matchbook_offer_reconciliation as reconciliation
from autosport.matchbook_offer_reconciliation import (
    MatchbookOfferPage, MatchbookOfferPageChain, MatchbookOfferReadback,
    MatchbookOfferReconciliationError, MatchbookOfferStatus, MatchbookOfferTruth,
    MatchbookRetryDisposition, MatchbookSettledBetStatus, reconcile_replay,
    reject_aggregated_offer_identity, require_distinct_settled_status, retry_disposition,
)

T0 = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)
A, B, SCOPE = "a" * 64, "b" * 64, "c" * 64


def offer(
    offer_id=1,
    *,
    status=MatchbookOfferStatus.OPEN,
    original="10",
    matched="0",
    remaining="10",
    at=T0,
    raw=A,
    issued=False,
):
    value = MatchbookOfferReadback(
        "acct",
        offer_id,
        status,
        Decimal(original),
        Decimal(matched),
        Decimal(remaining),
        at,
        raw,
    )
    if issued:
        return reconciliation._issue_provider_readback(value)
    return value


def page(offset, rows, *, per_page=2):
    return MatchbookOfferPage("acct", SCOPE, offset, per_page, tuple(rows), B)


def test_delayed_is_pending_and_non_authoritative():
    x = offer(status=MatchbookOfferStatus.DELAYED)
    assert x.truth is MatchbookOfferTruth.DELAYED_PENDING
    assert not x.settlement_authority and not x.provider_write_authority
    assert not x.provider_origin_authoritative
    assert (
        retry_disposition(
            observed_offer=x,
            expected_account_context_id=x.account_context_id,
            expected_offer_id=x.offer_id,
        )
        is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
    )

    issued = offer(status=MatchbookOfferStatus.DELAYED, issued=True)
    assert issued.provider_origin_authoritative
    assert (
        retry_disposition(
            observed_offer=issued,
            expected_account_context_id=issued.account_context_id,
            expected_offer_id=issued.offer_id,
        )
        is MatchbookRetryDisposition.DO_NOT_RETRY_ALREADY_OBSERVED
    )


def test_delayed_cannot_forge_matched_exposure():
    with pytest.raises(MatchbookOfferReconciliationError, match="delayed"):
        offer(status=MatchbookOfferStatus.DELAYED, matched="1", remaining="9")


def test_open_partial_and_full_match_truth():
    partial = offer(status=MatchbookOfferStatus.OPEN, matched="4", remaining="6")
    full = offer(status=MatchbookOfferStatus.MATCHED, matched="10", remaining="0")
    assert partial.truth is MatchbookOfferTruth.OPEN_PARTIALLY_MATCHED
    assert full.truth is MatchbookOfferTruth.FULLY_MATCHED
    with pytest.raises(MatchbookOfferReconciliationError, match="full match"):
        offer(status=MatchbookOfferStatus.MATCHED, matched="9", remaining="0")


@pytest.mark.parametrize("status", [MatchbookOfferStatus.CANCELLED, MatchbookOfferStatus.FLUSHED])
def test_cancel_flush_preserve_matched_prefix_and_close_remainder(status):
    x = offer(status=status, matched="4", remaining="0")
    assert x.truth is MatchbookOfferTruth.TERMINAL_PARTIAL_MATCH
    assert x.matched_stake == Decimal("4")
    with pytest.raises(MatchbookOfferReconciliationError, match="closes"):
        offer(status=status, matched="4", remaining="6")


def test_failed_does_not_carry_exposure():
    x = offer(status=MatchbookOfferStatus.FAILED, remaining="0")
    assert x.truth is MatchbookOfferTruth.SUBMISSION_FAILED
    with pytest.raises(MatchbookOfferReconciliationError, match="failed"):
        offer(status=MatchbookOfferStatus.FAILED, matched="1", remaining="0")


@pytest.mark.parametrize("code", [200, 400, 408, 409, 429, 500, 502, 503, 504])
def test_http_never_mints_blind_retry_authority(code):
    assert retry_disposition(http_status=code) is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY


def test_transport_exception_requires_reconciliation():
    assert retry_disposition(transport_exception=True) is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY


def test_observed_offer_requires_exact_submit_attempt_binding():
    x = offer(17, issued=True)

    assert (
        retry_disposition(observed_offer=x)
        is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
    )
    assert (
        retry_disposition(
            observed_offer=x,
            expected_account_context_id="other-account",
            expected_offer_id=17,
        )
        is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
    )
    assert (
        retry_disposition(
            observed_offer=x,
            expected_account_context_id=x.account_context_id,
            expected_offer_id=18,
        )
        is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
    )
    assert (
        retry_disposition(
            observed_offer=x,
            expected_account_context_id=x.account_context_id,
            expected_offer_id=17,
        )
        is MatchbookRetryDisposition.DO_NOT_RETRY_ALREADY_OBSERVED
    )


def test_caller_constructed_readback_cannot_mint_provider_origin():
    forged = offer(4242, status=MatchbookOfferStatus.OPEN, raw="f" * 64)
    assert not forged.provider_origin_authoritative
    assert (
        retry_disposition(
            observed_offer=forged,
            expected_account_context_id="acct",
            expected_offer_id=4242,
        )
        is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
    )


def test_copy_or_reconstruction_does_not_inherit_provider_origin():
    issued = offer(77, issued=True)
    copied = replace(issued)
    reconstructed = MatchbookOfferReadback(
        issued.account_context_id,
        issued.offer_id,
        issued.status,
        issued.original_stake,
        issued.matched_stake,
        issued.remaining_stake,
        issued.captured_at,
        issued.raw_response_sha256,
    )
    assert issued.provider_origin_authoritative
    assert not copied.provider_origin_authoritative
    assert not reconstructed.provider_origin_authoritative
    for detached in (copied, reconstructed):
        assert (
            retry_disposition(
                observed_offer=detached,
                expected_account_context_id=detached.account_context_id,
                expected_offer_id=detached.offer_id,
            )
            is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
        )


def test_serialized_restart_does_not_restore_provider_origin():
    issued = offer(88, issued=True)
    restored = pickle.loads(pickle.dumps(issued))
    assert issued.provider_origin_authoritative
    assert not restored.provider_origin_authoritative
    assert (
        retry_disposition(
            observed_offer=restored,
            expected_account_context_id=restored.account_context_id,
            expected_offer_id=restored.offer_id,
        )
        is MatchbookRetryDisposition.RECONCILE_BEFORE_RETRY
    )


def test_caller_constructed_readback_cannot_enter_authoritative_replay():
    forged = offer(
        4243,
        status=MatchbookOfferStatus.MATCHED,
        matched="10",
        remaining="0",
        raw="e" * 64,
    )
    with pytest.raises(
        MatchbookOfferReconciliationError,
        match="provider origin",
    ):
        reconcile_replay((forged,))

    issued = offer(
        4243,
        status=MatchbookOfferStatus.MATCHED,
        matched="10",
        remaining="0",
        raw="e" * 64,
        issued=True,
    )
    assert reconcile_replay((issued,)) == {4243: issued}


def test_detached_copy_cannot_regain_authoritative_replay_origin():
    issued = offer(4244, issued=True)
    detached = replace(issued)
    assert issued.provider_origin_authoritative
    assert not detached.provider_origin_authoritative
    with pytest.raises(
        MatchbookOfferReconciliationError,
        match="provider origin",
    ):
        reconcile_replay((detached,))


def test_aggregated_ids_are_not_singular_identity():
    with pytest.raises(MatchbookOfferReconciliationError, match="aggregated"):
        reject_aggregated_offer_identity(offer_id=None, offer_ids=(1, 2))
    assert reject_aggregated_offer_identity(offer_id=1, offer_ids=None) == 1


def test_full_page_is_not_absence_proof():
    chain = MatchbookOfferPageChain((page(0, [offer(1), offer(2)]),))
    assert not chain.pagination_closed
    assert not chain.traversal_absence_observed(999)
    assert not chain.provider_snapshot_atomicity_proven


def test_short_final_page_closes_only_traversal():
    chain = MatchbookOfferPageChain((page(0, [offer(1), offer(2)]), page(2, [offer(3)])))
    assert chain.pagination_closed and chain.traversal_absence_observed(999)
    assert not chain.provider_snapshot_atomicity_proven


def test_pagination_identity_and_offsets_fail_closed():
    with pytest.raises(MatchbookOfferReconciliationError, match="offset"):
        MatchbookOfferPageChain((page(0, [offer(1), offer(2)]), page(3, [offer(3)])))
    with pytest.raises(MatchbookOfferReconciliationError, match="terminal"):
        MatchbookOfferPageChain((page(0, [offer(1)]), page(1, [])))


def test_cross_page_duplicate_fails_closed():
    p1 = page(0, [offer(1, raw=A)], per_page=1)
    p2 = page(1, [offer(1, status=MatchbookOfferStatus.EDITED, raw=B)], per_page=1)
    with pytest.raises(MatchbookOfferReconciliationError, match="duplicate"):
        MatchbookOfferPageChain((p1, p2))


def test_replay_idempotence_and_same_time_conflict():
    x = offer(1, issued=True)
    assert reconcile_replay((x, x)) == {1: x}
    with pytest.raises(MatchbookOfferReconciliationError, match="same-time"):
        reconcile_replay((x, offer(1, status=MatchbookOfferStatus.EDITED, raw=B)))


def test_replay_preserves_matched_prefix_on_cancel():
    partial = offer(1, matched="4", remaining="6", issued=True)
    cancelled = offer(
        1,
        status=MatchbookOfferStatus.CANCELLED,
        matched="4",
        remaining="0",
        at=T0 + timedelta(seconds=1),
        raw=B,
        issued=True,
    )
    latest = reconcile_replay((partial, cancelled))[1]
    assert latest.truth is MatchbookOfferTruth.TERMINAL_PARTIAL_MATCH
    assert latest.matched_stake == Decimal("4")


def test_replay_rejects_time_matched_and_terminal_regressions():
    later = offer(1, at=T0 + timedelta(seconds=1))
    with pytest.raises(MatchbookOfferReconciliationError, match="backwards"):
        reconcile_replay((later, offer(1)))
    partial = offer(1, matched="4", remaining="6")
    regressed = offer(1, matched="3", remaining="7", at=T0 + timedelta(seconds=1), raw=B)
    with pytest.raises(MatchbookOfferReconciliationError, match="decreased"):
        reconcile_replay((partial, regressed))
    cancelled = offer(1, status=MatchbookOfferStatus.CANCELLED, matched="4", remaining="0")
    reopened = offer(1, matched="4", remaining="6", at=T0 + timedelta(seconds=1), raw=B)
    with pytest.raises(MatchbookOfferReconciliationError, match="regressed"):
        reconcile_replay((cancelled, reopened))


def test_push_variants_remain_distinct():
    values = [require_distinct_settled_status(x) for x in ("PUSH", "PUSH_WIN", "PUSH_LOSE")]
    assert values == [MatchbookSettledBetStatus.PUSH, MatchbookSettledBetStatus.PUSH_WIN, MatchbookSettledBetStatus.PUSH_LOSE]
    with pytest.raises(MatchbookOfferReconciliationError, match="unsupported"):
        require_distinct_settled_status("VOID")


def test_decimal_fingerprint_is_context_independent():
    x = offer(original="123456789.123456789", matched="23.000000001", remaining="100.123456788")
    expected = x.fingerprint()
    with localcontext() as ctx:
        ctx.prec = 4
        assert x.fingerprint() == expected


def test_nonfinite_overlapping_and_naive_time_fail_closed():
    with pytest.raises(MatchbookOfferReconciliationError, match="finite"):
        offer(original="NaN")
    with pytest.raises(MatchbookOfferReconciliationError, match="inconsistent"):
        offer(matched="7", remaining="4")
    with pytest.raises(MatchbookOfferReconciliationError, match="timezone-aware"):
        offer(at=datetime(2026, 9, 22, 1, 0))
