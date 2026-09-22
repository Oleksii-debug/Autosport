from dataclasses import replace
from decimal import Decimal, getcontext
import json

import pytest

from autosport.prophetx_replace_reconciliation import (
    Fill,
    OrderStatus,
    OrderType,
    Replaced,
    ReplaceOutcome,
    ReplaceReject,
    ReplaceRequest,
    Transport,
    WorkingOrder,
    ProphetXReplaceConflict,
    ProphetXReplaceError,
    ProphetXReplaceUnsupported,
    decode_checkpoint,
    encode_checkpoint,
    reconcile,
)


def request(**kw):
    d = dict(
        canonical_order_id="canon-1",
        account_id="acct",
        symbol="SYM",
        side="BUY",
        provider_order_id="px-1",
        original_cl_ord_id="orig-1",
        replace_cl_ord_id="repl-1",
        new_price="2.25",
        new_quantity="8",
        requested_at="2026-09-22T20:00:02Z",
    )
    d.update(kw)
    return ReplaceRequest(**d)


def working(**kw):
    d = dict(
        account_id="acct",
        symbol="SYM",
        side="BUY",
        provider_order_id="px-1",
        cl_ord_id="orig-1",
        status=OrderStatus.NEW,
        order_quantity="10",
        cumulative_filled="0",
        leaves_quantity="10",
        average_fill_price=None,
        observed_at="2026-09-22T20:00:01Z",
        fix_session_id="sess",
        fix_sequence=10,
    )
    d.update(kw)
    return WorkingOrder(**d)


def replaced(**kw):
    d = dict(
        exec_id="exec-r",
        account_id="acct",
        symbol="SYM",
        side="BUY",
        provider_order_id="px-1",
        cl_ord_id="repl-1",
        orig_cl_ord_id="orig-1",
        price="2.25",
        order_quantity="8",
        cumulative_quantity="0",
        leaves_quantity="8",
        transact_time="2026-09-22T20:00:03Z",
        fix_session_id="sess",
        fix_sequence=12,
    )
    d.update(kw)
    return Replaced(**d)


def fill(exec_id, cl_ord_id, last, px, cum, leaves, seq):
    return Fill(
        exec_id=exec_id,
        account_id="acct",
        symbol="SYM",
        side="BUY",
        provider_order_id="px-1",
        cl_ord_id=cl_ord_id,
        last_quantity=last,
        last_price=px,
        cumulative_quantity=cum,
        leaves_quantity=leaves,
        transact_time=f"2026-09-22T20:00:{seq:02d}Z",
        fix_session_id="sess",
        fix_sequence=seq,
    )


def reject(**kw):
    d = dict(
        reject_id="rej-1",
        account_id="acct",
        symbol="SYM",
        side="BUY",
        provider_order_id="px-1",
        replace_cl_ord_id="repl-1",
        orig_cl_ord_id="orig-1",
        reason="too late",
        order_status=OrderStatus.PARTIALLY_FILLED,
        transact_time="2026-09-22T20:00:04Z",
        fix_session_id="sess",
        fix_sequence=13,
    )
    d.update(kw)
    return ReplaceReject(**d)


def test_unfilled_successful_fix_replace():
    p = reconcile(request(), working(), [replaced()])
    assert p.outcome is ReplaceOutcome.REPLACED
    assert p.original_filled_quantity == 0
    assert p.replacement_open_quantity == Decimal("8")
    assert p.active_cl_ord_id == "repl-1"
    assert p.original_status is OrderStatus.REPLACED


def test_old_partial_fill_survives_cumqty_reset():
    w = working(
        status=OrderStatus.PARTIALLY_FILLED,
        cumulative_filled="2",
        leaves_quantity="8",
        average_fill_price="2.0",
    )
    p = reconcile(request(), w, [replaced()])
    assert p.original_filled_quantity == Decimal("2")
    assert p.original_average_fill_price == Decimal("2.0")
    assert p.total_historical_filled_quantity == Decimal("2")


def test_fill_before_replace_is_added_to_old_history():
    f = fill("old-fill", "orig-1", "2", "2.10", "2", "8", 11)
    p = reconcile(request(), working(), [replaced(), f])
    assert p.original_filled_quantity == Decimal("2")
    assert p.original_average_fill_price == Decimal("2.10")


def test_replacement_fill_after_replace_preserves_both_lifecycles():
    w = working(
        status=OrderStatus.PARTIALLY_FILLED,
        cumulative_filled="2",
        leaves_quantity="8",
        average_fill_price="2.0",
    )
    nf = fill("new-fill", "repl-1", "3", "2.30", "3", "5", 13)
    p = reconcile(request(), w, [nf, replaced()])
    assert p.original_filled_quantity == Decimal("2")
    assert p.replacement_filled_quantity == Decimal("3")
    assert p.replacement_open_quantity == Decimal("5")
    assert p.total_historical_filled_quantity == Decimal("5")


def test_replacement_fill_before_replaced_fails():
    nf = fill("new-fill", "repl-1", "3", "2.30", "3", "5", 11)
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [nf, replaced(fix_sequence=12)],
        )


def test_pending_new_cannot_be_locally_promoted_to_working():
    with pytest.raises(ProphetXReplaceError):
        reconcile(
            request(),
            working(status=OrderStatus.PENDING_NEW),
            [],
        )


def test_working_evidence_cannot_be_retroactively_acquired():
    with pytest.raises(ProphetXReplaceError):
        reconcile(
            request(requested_at="2026-09-22T20:00:00Z"),
            working(),
            [],
        )


def test_market_order_replace_is_unsupported():
    with pytest.raises(ProphetXReplaceUnsupported):
        reconcile(
            request(order_type=OrderType.MARKET),
            working(),
            [],
        )


def test_rest_atomic_replace_is_explicitly_unsupported():
    with pytest.raises(ProphetXReplaceUnsupported):
        reconcile(
            request(transport=Transport.REST),
            working(),
            [],
        )


def test_wrong_orig_clordid_fails_closed():
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [replaced(orig_cl_ord_id="other")],
        )


def test_wrong_provider_order_id_fails_closed():
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [replaced(provider_order_id="other")],
        )


def test_replaced_cumqty_must_reset_to_zero():
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [
                replaced(
                    cumulative_quantity="1",
                    leaves_quantity="7",
                )
            ],
        )


def test_replaced_economics_must_match_frozen_request():
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [replaced(price="2.30")],
        )


def test_exact_execid_replay_is_idempotent():
    r = replaced()
    assert reconcile(
        request(),
        working(),
        [r, r],
    ) == reconcile(request(), working(), [r])


def test_conflicting_execid_replay_fails():
    r = replaced()
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [r, replace(r, price=Decimal("2.30"))],
        )


def test_same_fix_sequence_different_evidence_fails():
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [replaced(), reject(fix_sequence=12)],
        )


def test_stale_sequence_and_wrong_session_fail():
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [replaced(fix_sequence=10)],
        )
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [replaced(fix_session_id="other")],
        )


def test_replace_reject_does_not_apply_requested_economics():
    p = reconcile(
        request(),
        working(),
        [reject(order_status=OrderStatus.NEW)],
    )
    assert p.outcome is ReplaceOutcome.REJECTED
    assert p.active_cl_ord_id == "orig-1"
    assert p.active_price is None
    assert p.requires_readback is True
    assert p.replacement_open_quantity == 0


def test_reject_after_old_fill_preserves_fill():
    f = fill("old-fill", "orig-1", "2", "2.10", "2", "8", 11)
    p = reconcile(
        request(),
        working(),
        [f, reject(fix_sequence=12)],
    )
    assert p.outcome is ReplaceOutcome.REJECTED
    assert p.original_filled_quantity == Decimal("2")


def test_success_and_reject_are_mutually_incompatible():
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [replaced(), reject(fix_sequence=13)],
        )


def test_old_fill_after_replaced_fails():
    f = fill("late-old", "orig-1", "1", "2.1", "1", "9", 13)
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [replaced(), f],
        )


def test_fill_conservation_is_fail_closed():
    f = fill("bad", "orig-1", "2", "2.1", "2", "7", 11)
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(request(), working(), [f])


def test_full_fill_race_before_replaced_fails_closed():
    old_fill = fill("fill-all", "orig-1", "10", "2.10", "10", "0", 11)
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(request(), working(), [old_fill, replaced()])


def test_checkpoint_is_deterministic_restart_cache():
    p = reconcile(request(), working(), [replaced()])
    raw = encode_checkpoint(p)
    assert decode_checkpoint(raw) == p
    assert encode_checkpoint(decode_checkpoint(raw)) == raw
    assert "CACHE_ONLY_RE_RESOLVE_PROVIDER_EVIDENCE" in raw


def test_checkpoint_tamper_and_duplicate_keys_fail():
    p = reconcile(request(), working(), [replaced()])
    raw = encode_checkpoint(p)
    obj = json.loads(raw)
    obj["payload"]["active_cl_ord_id"] = "tampered"
    with pytest.raises(ProphetXReplaceConflict):
        decode_checkpoint(json.dumps(obj))
    with pytest.raises(ProphetXReplaceConflict):
        decode_checkpoint(
            '{"schema_version":1,"schema_version":1,'
            '"authority":"x","payload":{},'
            '"payload_sha256":"x"}'
        )


def test_decimal_context_does_not_change_projection_identity():
    old = getcontext().prec
    try:
        getcontext().prec = 6
        a = reconcile(
            request(new_price="2.250000"),
            working(),
            [replaced(price="2.250000")],
        )
        getcontext().prec = 50
        b = reconcile(
            request(new_price="2.250000"),
            working(),
            [replaced(price="2.250000")],
        )
    finally:
        getcontext().prec = old
    assert a == b


def test_weighted_average_is_exact_and_deterministic():
    f1 = fill("f1", "orig-1", "2", "2.10", "2", "8", 11)
    f2 = fill("f2", "orig-1", "3", "2.20", "5", "5", 12)
    p = reconcile(request(), working(), [f2, f1])
    assert p.original_filled_quantity == Decimal("5")
    assert p.original_average_fill_price == Decimal("2.16")


def test_request_identity_binds_new_economics():
    assert request().fingerprint != request(new_quantity="7").fingerprint
    assert request().fingerprint != request(new_price="2.30").fingerprint


def test_cross_account_and_order_evidence_rejected():
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(),
            [replaced(account_id="other")],
        )
    with pytest.raises(ProphetXReplaceConflict):
        reconcile(
            request(),
            working(account_id="other"),
            [],
        )


def test_working_order_conservation_and_average_rules():
    with pytest.raises(ProphetXReplaceError):
        working(
            cumulative_filled="1",
            leaves_quantity="8",
            average_fill_price="2",
        )
    with pytest.raises(ProphetXReplaceError):
        working(
            cumulative_filled="1",
            leaves_quantity="9",
            average_fill_price=None,
        )
    with pytest.raises(ProphetXReplaceError):
        working(
            status=OrderStatus.NEW,
            cumulative_filled="1",
            leaves_quantity="9",
            average_fill_price="2",
        )
    with pytest.raises(ProphetXReplaceError):
        working(
            status=OrderStatus.PARTIALLY_FILLED,
            cumulative_filled="0",
            leaves_quantity="10",
            average_fill_price=None,
        )
