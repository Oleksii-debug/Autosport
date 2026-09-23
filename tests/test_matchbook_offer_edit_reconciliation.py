from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from autosport.matchbook_offer_edit_reconciliation import (
    MatchbookOfferEditFailureReason,
    MatchbookOfferEditIdentityDisposition,
    MatchbookOfferEditIntent,
    MatchbookOfferEditReadback,
    MatchbookOfferEditReconciliationError,
    MatchbookOfferEditRetryDisposition,
    MatchbookOfferEditStatus,
    MatchbookOfferEditTruth,
    identity_disposition,
    reconcile_offer_edit_replay,
    retry_disposition,
)

T0 = datetime(2026, 9, 22, 3, 45, tzinfo=timezone.utc)
RAW_A = "a" * 64
RAW_B = "b" * 64
RAW_C = "c" * 64


def intent(**kwargs):
    values = dict(
        account_context_id="acct",
        offer_id=101,
        current_odds=Decimal("2.00"),
        current_stake=Decimal("50.00"),
        new_odds=Decimal("2.10"),
        new_stake=Decimal("45.00"),
        requested_at=T0,
    )
    values.update(kwargs)
    return MatchbookOfferEditIntent(**values)


def readback(
    *,
    status=MatchbookOfferEditStatus.DELAYED,
    offer_id=101,
    edit_id=9001,
    account="acct",
    at=T0 + timedelta(seconds=1),
    raw=RAW_A,
    delay=Decimal("5"),
    reason=None,
):
    if status is not MatchbookOfferEditStatus.DELAYED and delay == Decimal("5"):
        delay = None
    if status is MatchbookOfferEditStatus.FAILED and reason is None:
        reason = MatchbookOfferEditFailureReason.DELAY_TIMEOUT
    return MatchbookOfferEditReadback(
        account_context_id=account,
        offer_id=offer_id,
        offer_edit_id=edit_id,
        status=status,
        captured_at=at,
        raw_response_sha256=raw,
        delay_seconds=delay,
        failure_reason=reason,
    )


def test_intent_binds_exact_current_and_new_terms_without_write_authority():
    x = intent()
    assert not x.provider_write_authority
    assert x.current_odds == Decimal("2.00")
    assert x.new_stake == Decimal("45.00")
    assert len(x.fingerprint()) == 64


def test_noop_edit_is_rejected():
    with pytest.raises(MatchbookOfferEditReconciliationError, match="must change"):
        intent(new_odds=Decimal("2.00"), new_stake=Decimal("50.00"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_odds", Decimal("0")),
        ("current_stake", Decimal("0")),
        ("new_odds", Decimal("NaN")),
        ("new_stake", Decimal("Infinity")),
    ],
)
def test_nonpositive_or_nonfinite_request_terms_fail_closed(field, value):
    with pytest.raises(MatchbookOfferEditReconciliationError):
        intent(**{field: value})


@pytest.mark.parametrize("control", ("\\x00", "\\t", "\\n", "\\r", "\\x1f", "\\x7f"))
def test_intent_account_context_rejects_ascii_control_characters(control):
    with pytest.raises(MatchbookOfferEditReconciliationError, match="canonical text"):
        intent(account_context_id=f"acct{control}alias")


@pytest.mark.parametrize("control", ("\\x00", "\\t", "\\n", "\\r", "\\x1f", "\\x7f"))
def test_readback_account_context_rejects_ascii_control_characters(control):
    with pytest.raises(MatchbookOfferEditReconciliationError, match="canonical text"):
        readback(account=f"acct{control}alias")


def test_delayed_is_pending_not_applied_and_has_no_authority():
    x = readback()
    assert x.truth is MatchbookOfferEditTruth.PENDING_DELAY_ASSERTION
    assert x.provider_origin_verified is False
    assert not x.provider_write_authority
    assert not x.settlement_authority
    assert not x.retry_authority


def test_delayed_requires_positive_delay_and_no_failure_reason():
    with pytest.raises(MatchbookOfferEditReconciliationError, match="positive delay"):
        readback(delay=Decimal("0"))
    with pytest.raises(MatchbookOfferEditReconciliationError, match="non-failed"):
        readback(reason=MatchbookOfferEditFailureReason.SERVER_ERROR)


def test_applied_is_observed_but_not_write_or_settlement_authority():
    x = readback(status=MatchbookOfferEditStatus.APPLIED)
    assert x.truth is MatchbookOfferEditTruth.APPLIED_ASSERTION_UNVERIFIED
    assert x.provider_origin_verified is False
    assert not x.provider_write_authority and not x.settlement_authority


@pytest.mark.parametrize("reason", list(MatchbookOfferEditFailureReason))
def test_failed_preserves_documented_failure_reason(reason):
    x = readback(status=MatchbookOfferEditStatus.FAILED, reason=reason)
    assert x.truth is MatchbookOfferEditTruth.FAILED_ASSERTION_UNVERIFIED
    assert x.provider_origin_verified is False
    assert x.failure_reason is reason
    assert not x.retry_authority


def test_failed_requires_provider_reason():
    with pytest.raises(MatchbookOfferEditReconciliationError, match="requires"):
        MatchbookOfferEditReadback(
            account_context_id="acct",
            offer_id=101,
            offer_edit_id=9001,
            status=MatchbookOfferEditStatus.FAILED,
            captured_at=T0,
            raw_response_sha256=RAW_A,
        )


def test_unknown_edit_id_never_becomes_identity_from_similarity():
    assert identity_disposition(offer_edit_id=None) is MatchbookOfferEditIdentityDisposition.UNKNOWN_NO_EDIT_ID_FAIL_CLOSED
    assert identity_disposition(offer_edit_id=9001) is MatchbookOfferEditIdentityDisposition.EDIT_ID_PRESENT_REQUIRES_PROVIDER_ORIGIN
    for bad in (0, -1, True, "9001"):
        with pytest.raises(MatchbookOfferEditReconciliationError):
            identity_disposition(offer_edit_id=bad)


@pytest.mark.parametrize("code", [200, 400, 408, 409, 429, 500, 502, 503, 504])
def test_http_result_alone_never_authorizes_repeat(code):
    assert retry_disposition(http_status=code) is MatchbookOfferEditRetryDisposition.RECONCILE_BEFORE_ANY_NEW_EDIT


def test_transport_exception_without_readback_requires_reconciliation():
    assert retry_disposition(transport_exception=True) is MatchbookOfferEditRetryDisposition.RECONCILE_BEFORE_ANY_NEW_EDIT


def test_detached_delayed_readback_cannot_mint_wait_authority():
    detached = readback()
    assert detached.provider_origin_verified is False
    assert (
        retry_disposition(readback=detached)
        is MatchbookOfferEditRetryDisposition.RECONCILE_BEFORE_ANY_NEW_EDIT
    )


@pytest.mark.parametrize("status", [MatchbookOfferEditStatus.APPLIED, MatchbookOfferEditStatus.FAILED])
def test_detached_terminal_readback_cannot_mint_repeat_suppression(status):
    detached = readback(status=status)
    assert detached.provider_origin_verified is False
    assert (
        retry_disposition(readback=detached)
        is MatchbookOfferEditRetryDisposition.RECONCILE_BEFORE_ANY_NEW_EDIT
    )


def test_replay_delayed_to_applied_is_valid():
    delayed = readback()
    applied = readback(
        status=MatchbookOfferEditStatus.APPLIED,
        at=T0 + timedelta(seconds=6),
        raw=RAW_B,
    )
    assert reconcile_offer_edit_replay((delayed, applied))[delayed.scoped_identity] == applied


def test_replay_delayed_to_failed_is_valid_and_preserves_reason():
    delayed = readback()
    failed = readback(
        status=MatchbookOfferEditStatus.FAILED,
        at=T0 + timedelta(seconds=6),
        raw=RAW_B,
        reason=MatchbookOfferEditFailureReason.MARKET_STATUS,
    )
    latest = reconcile_offer_edit_replay((delayed, failed))[delayed.scoped_identity]
    assert latest.status is MatchbookOfferEditStatus.FAILED
    assert latest.failure_reason is MatchbookOfferEditFailureReason.MARKET_STATUS


@pytest.mark.parametrize(
    "first,second",
    [
        (MatchbookOfferEditStatus.APPLIED, MatchbookOfferEditStatus.FAILED),
        (MatchbookOfferEditStatus.FAILED, MatchbookOfferEditStatus.APPLIED),
        (MatchbookOfferEditStatus.APPLIED, MatchbookOfferEditStatus.DELAYED),
        (MatchbookOfferEditStatus.FAILED, MatchbookOfferEditStatus.DELAYED),
    ],
)
def test_terminal_status_cannot_change_without_correction_authority(first, second):
    x = readback(status=first)
    y = readback(status=second, at=T0 + timedelta(seconds=2), raw=RAW_B)
    with pytest.raises(MatchbookOfferEditReconciliationError, match="terminal"):
        reconcile_offer_edit_replay((x, y))


def test_replay_rejects_time_rollback_and_same_time_conflict():
    later = readback(at=T0 + timedelta(seconds=3))
    earlier = readback(at=T0 + timedelta(seconds=2), raw=RAW_B)
    with pytest.raises(MatchbookOfferEditReconciliationError, match="backwards"):
        reconcile_offer_edit_replay((later, earlier))

    x = readback(at=T0 + timedelta(seconds=3))
    y = readback(at=T0 + timedelta(seconds=3), raw=RAW_B)
    with pytest.raises(MatchbookOfferEditReconciliationError, match="same-time"):
        reconcile_offer_edit_replay((x, y))


def test_replay_scopes_same_numeric_edit_id_by_account_and_offer():
    base = readback()
    other_account = readback(
        account="other",
        at=T0 + timedelta(seconds=2),
        raw=RAW_B,
    )
    other_offer = readback(
        offer_id=102,
        at=T0 + timedelta(seconds=2),
        raw=RAW_C,
    )
    latest = reconcile_offer_edit_replay((base, other_account, other_offer))
    assert latest[base.scoped_identity] == base
    assert latest[other_account.scoped_identity] == other_account
    assert latest[other_offer.scoped_identity] == other_offer
    assert len(latest) == 3


def test_delayed_repeat_cannot_rewrite_delay_evidence():
    x = readback(delay=Decimal("5"))
    y = readback(delay=Decimal("6"), at=T0 + timedelta(seconds=2), raw=RAW_B)
    with pytest.raises(MatchbookOfferEditReconciliationError, match="changed delay"):
        reconcile_offer_edit_replay((x, y))


def test_terminal_repeat_may_refresh_raw_bytes_but_not_semantics():
    x = readback(status=MatchbookOfferEditStatus.APPLIED, at=T0 + timedelta(seconds=2), raw=RAW_A)
    y = readback(status=MatchbookOfferEditStatus.APPLIED, at=T0 + timedelta(seconds=3), raw=RAW_B)
    assert reconcile_offer_edit_replay((x, y))[x.scoped_identity] == y

    f1 = readback(
        status=MatchbookOfferEditStatus.FAILED,
        at=T0 + timedelta(seconds=2),
        raw=RAW_A,
        reason=MatchbookOfferEditFailureReason.SERVER_ERROR,
    )
    f2 = readback(
        status=MatchbookOfferEditStatus.FAILED,
        at=T0 + timedelta(seconds=3),
        raw=RAW_C,
        reason=MatchbookOfferEditFailureReason.DELAY_TIMEOUT,
    )
    with pytest.raises(MatchbookOfferEditReconciliationError, match="failure reason"):
        reconcile_offer_edit_replay((f1, f2))


def test_decimal_fingerprints_are_context_independent():
    i = intent(
        current_odds=Decimal("123456789.123456789"),
        new_odds=Decimal("123456789.223456789"),
    )
    r = readback(delay=Decimal("123456789.123456789"))
    expected_i = i.fingerprint()
    expected_r = r.fingerprint()
    with localcontext() as ctx:
        ctx.prec = 4
        assert i.fingerprint() == expected_i
        assert r.fingerprint() == expected_r


def test_naive_times_and_wrong_types_fail_closed():
    with pytest.raises(MatchbookOfferEditReconciliationError, match="timezone-aware"):
        intent(requested_at=datetime(2026, 9, 22, 3, 45))
    with pytest.raises(MatchbookOfferEditReconciliationError, match="status"):
        MatchbookOfferEditReadback(
            account_context_id="acct",
            offer_id=101,
            offer_edit_id=9001,
            status="delayed",  # type: ignore[arg-type]
            captured_at=T0,
            raw_response_sha256=RAW_A,
            delay_seconds=Decimal("5"),
        )
