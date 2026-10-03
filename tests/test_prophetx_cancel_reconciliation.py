from dataclasses import replace
from decimal import Decimal
import json

import pytest

from autosport.prophetx_cancel_reconciliation import (
    CancelCause,
    CancelOutcome,
    CancelReject,
    CancelRejectReason,
    CancelRequest,
    CancelScope,
    CancelTransport,
    CanceledReport,
    Fill,
    OrderStatus,
    ProphetXCancelConflict,
    ProphetXCancelError,
    RestTransportObservation,
    RestTransportState,
    WorkingOrder,
    decode_checkpoint,
    encode_checkpoint,
    reconcile_cancel,
)


def request(**kw):
    d = dict(
        canonical_order_id="canon-1",
        environment="sandbox",
        account_id="acct",
        symbol="SYM",
        side="BUY",
        provider_order_id="px-1",
        original_cl_ord_id="orig-1",
        cancel_cl_ord_id="cancel-1",
        requested_at="2026-09-22T20:00:02Z",
        transport=CancelTransport.FIX,
    )
    d.update(kw)
    if d["transport"] is CancelTransport.REST and "cancel_cl_ord_id" not in kw:
        d["cancel_cl_ord_id"] = None
    return CancelRequest(**d)


def working(**kw):
    d = dict(
        environment="sandbox",
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


def fill(
    exec_id="fill-1",
    last="2",
    px="2.10",
    cum="2",
    leaves="8",
    seq=11,
    **kw,
):
    d = dict(
        exec_id=exec_id,
        environment="sandbox",
        account_id="acct",
        symbol="SYM",
        side="BUY",
        provider_order_id="px-1",
        cl_ord_id="orig-1",
        last_quantity=last,
        last_price=px,
        cumulative_quantity=cum,
        leaves_quantity=leaves,
        transact_time=f"2026-09-22T20:00:{seq:02d}Z",
        fix_session_id="sess",
        fix_sequence=seq,
    )
    d.update(kw)
    return Fill(**d)


def canceled(
    exec_id="cancel-report-1",
    cum="0",
    seq=12,
    **kw,
):
    d = dict(
        exec_id=exec_id,
        environment="sandbox",
        account_id="acct",
        symbol="SYM",
        side="BUY",
        provider_order_id="px-1",
        cl_ord_id="orig-1",
        cumulative_quantity=cum,
        leaves_quantity="0",
        transact_time=f"2026-09-22T20:00:{seq:02d}Z",
        fix_session_id="sess",
        fix_sequence=seq,
    )
    d.update(kw)
    return CanceledReport(**d)


def reject(
    order_status=OrderStatus.NEW,
    seq=12,
    **kw,
):
    d = dict(
        reject_id="reject-1",
        environment="sandbox",
        account_id="acct",
        symbol="SYM",
        side="BUY",
        provider_order_id="px-1",
        cancel_cl_ord_id="cancel-1",
        orig_cl_ord_id="orig-1",
        reason=CancelRejectReason.TOO_LATE,
        order_status=order_status,
        transact_time=f"2026-09-22T20:00:{seq:02d}Z",
        fix_session_id="sess",
        fix_sequence=seq,
    )
    d.update(kw)
    return CancelReject(**d)


def rest(
    http_status=None,
    state=RestTransportState.TIMEOUT_AFTER_POSSIBLE_SEND,
    **kw,
):
    d = dict(
        observation_id="rest-1",
        environment="sandbox",
        account_id="acct",
        provider_order_id="px-1",
        http_status=http_status,
        state=state,
        observed_at="2026-09-22T20:00:03Z",
    )
    d.update(kw)
    return RestTransportObservation(**d)


def test_unfilled_fix_cancel_releases_only_known_open_quantity():
    p = reconcile_cancel(
        working(),
        [canceled()],
        request=request(),
    )
    assert p.outcome is CancelOutcome.TERMINAL_OBSERVED
    assert p.provider_status is OrderStatus.CANCELED
    assert p.known_filled_quantity == 0
    assert p.known_open_quantity == 0
    assert p.released_quantity == Decimal("10")
    assert p.cause is CancelCause.PROVIDER_TERMINAL_UNSPECIFIED


def test_partial_fill_then_cancel_preserves_filled_exposure():
    w = working(
        status=OrderStatus.PARTIALLY_FILLED,
        cumulative_filled="2",
        leaves_quantity="8",
        average_fill_price="2.0",
    )
    p = reconcile_cancel(
        w,
        [canceled(cum="2")],
        request=request(),
    )
    assert p.known_filled_quantity == Decimal("2")
    assert p.known_average_fill_price == Decimal("2.0")
    assert p.released_quantity == Decimal("8")
    assert p.known_open_quantity == 0


def test_fill_race_before_cancel_confirmation_remains_authoritative():
    p = reconcile_cancel(
        working(),
        [
            canceled(cum="2", seq=12),
            fill(seq=11),
        ],
        request=request(),
    )
    assert p.known_filled_quantity == Decimal("2")
    assert p.known_average_fill_price == Decimal("2.10")
    assert p.released_quantity == Decimal("8")


def test_full_fill_wins_race_and_cannot_be_relabelled_canceled():
    f = fill(
        last="10",
        cum="10",
        leaves="0",
        seq=11,
    )
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                f,
                canceled(cum="10", seq=12),
            ],
            request=request(),
        )


@pytest.mark.parametrize(
    ("http_status", "state"),
    [
        (None, RestTransportState.TIMEOUT_AFTER_POSSIBLE_SEND),
        (404, RestTransportState.HTTP_404_SAMPLE_AMBIGUOUS),
        (200, RestTransportState.MALFORMED_OR_UNQUALIFIED_RESPONSE),
    ],
)
def test_rest_transport_result_never_mints_terminal_truth(
    http_status,
    state,
):
    p = reconcile_cancel(
        working(),
        [
            rest(
                http_status=http_status,
                state=state,
            )
        ],
        request=request(
            transport=CancelTransport.REST,
        ),
    )
    assert p.outcome is CancelOutcome.UNKNOWN
    assert p.provider_status is OrderStatus.NEW
    assert p.known_open_quantity == Decimal("10")
    assert p.released_quantity == 0
    assert p.requires_readback is True
    assert p.cause is CancelCause.TRANSPORT_AMBIGUOUS


def test_fix_canceled_correlates_to_original_order_clordid_not_cancel_request():
    assert (
        reconcile_cancel(
            working(),
            [canceled(cl_ord_id="orig-1")],
            request=request(),
        ).provider_status
        is OrderStatus.CANCELED
    )
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [canceled(cl_ord_id="cancel-1")],
            request=request(),
        )


def test_wrong_provider_order_id_fails_closed_even_if_display_fields_match():
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [canceled(provider_order_id="other")],
            request=request(),
        )


def test_cancel_reject_too_late_filled_preserves_observed_fill_and_releases_nothing():
    f = fill(
        last="10",
        cum="10",
        leaves="0",
        seq=11,
    )
    p = reconcile_cancel(
        working(),
        [
            f,
            reject(
                order_status=OrderStatus.FILLED,
                seq=12,
            ),
        ],
        request=request(),
    )
    assert p.outcome is CancelOutcome.REJECTED
    assert p.provider_status is OrderStatus.FILLED
    assert p.known_filled_quantity == Decimal("10")
    assert p.known_open_quantity is None
    assert p.released_quantity == 0
    assert p.requires_readback is True


def test_cancel_reject_unknown_order_does_not_infer_zero_effect():
    p = reconcile_cancel(
        working(),
        [
            reject(
                order_status=OrderStatus.UNKNOWN,
            )
        ],
        request=request(),
    )
    assert p.outcome is CancelOutcome.REJECTED
    assert p.provider_status is OrderStatus.UNKNOWN
    assert p.known_open_quantity is None
    assert p.released_quantity == 0
    assert p.requires_readback is True


def test_cancel_reject_other_working_status_keeps_exposure():
    p = reconcile_cancel(
        working(),
        [
            reject(
                order_status=OrderStatus.NEW,
            )
        ],
        request=request(),
    )
    assert p.provider_status is OrderStatus.NEW
    assert p.known_open_quantity == Decimal("10")
    assert p.released_quantity == 0


def test_venue_originated_cancel_without_local_request_is_terminal_but_not_user_cancel():
    p = reconcile_cancel(
        working(),
        [canceled()],
    )
    assert p.outcome is CancelOutcome.NOT_REQUESTED
    assert p.provider_status is OrderStatus.CANCELED
    assert p.cause is CancelCause.PROVIDER_TERMINAL_UNSPECIFIED
    assert "USER" not in p.cause.value
    assert p.released_quantity == Decimal("10")


def test_self_match_wipe_like_provider_cancel_never_gets_user_cause_label():
    p = reconcile_cancel(
        working(),
        [
            canceled(
                exec_id="wipe-like",
            )
        ],
    )
    assert p.cause is CancelCause.PROVIDER_TERMINAL_UNSPECIFIED
    assert "USER_CANCEL" not in p.cause.value


def test_exact_canceled_report_replay_is_idempotent():
    x = canceled()
    a = reconcile_cancel(
        working(),
        [x, x],
        request=request(),
    )
    b = reconcile_cancel(
        working(),
        [x],
        request=request(),
    )
    assert a == b


def test_conflicting_provider_evidence_id_replay_fails():
    x = canceled()
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                x,
                replace(
                    x,
                    cumulative_quantity=Decimal("1"),
                ),
            ],
            request=request(),
        )


def test_restart_after_cancel_request_before_confirmation_is_same_pending_attempt():
    p = reconcile_cancel(
        working(),
        [],
        request=request(),
    )
    assert p.outcome is CancelOutcome.PENDING
    raw = encode_checkpoint(p)
    assert decode_checkpoint(raw) == p
    assert encode_checkpoint(decode_checkpoint(raw)) == raw
    assert "CACHE_ONLY_RE_RESOLVE_PROVIDER_EVIDENCE" in raw


def test_restart_after_partial_fill_and_cancel_preserves_fill_history():
    f = fill()
    p = reconcile_cancel(
        working(),
        [
            f,
            canceled(cum="2"),
        ],
        request=request(),
    )
    restored = decode_checkpoint(
        encode_checkpoint(p)
    )
    assert restored == p
    assert restored.known_filled_quantity == Decimal("2")
    assert restored.released_quantity == Decimal("8")


def test_batch_like_mixed_orders_are_reconciled_independently():
    success = reconcile_cancel(
        working(),
        [canceled()],
        request=request(),
    )
    rejected = reconcile_cancel(
        working(
            provider_order_id="px-2",
            cl_ord_id="orig-2",
        ),
        [
            reject(
                provider_order_id="px-2",
                orig_cl_ord_id="orig-2",
                cancel_cl_ord_id="cancel-2",
            )
        ],
        request=request(
            provider_order_id="px-2",
            original_cl_ord_id="orig-2",
            cancel_cl_ord_id="cancel-2",
        ),
    )
    assert success.provider_status is OrderStatus.CANCELED
    assert rejected.outcome is CancelOutcome.REJECTED


@pytest.mark.parametrize(
    "field",
    [
        "environment",
        "account_id",
        "provider_order_id",
    ],
)
def test_identity_mismatch_fails_closed(field):
    kw = {field: "other"}
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [canceled(**kw)],
            request=request(),
        )


def test_request_environment_account_mismatch_fails_before_evidence():
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [],
            request=request(
                environment="production",
            ),
        )
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [],
            request=request(
                account_id="other",
            ),
        )


def test_working_evidence_cannot_be_observed_after_cancel_request():
    with pytest.raises(ProphetXCancelError):
        reconcile_cancel(
            working(),
            [],
            request=request(
                requested_at="2026-09-22T20:00:00Z",
            ),
        )


def test_fix_session_and_sequence_are_causal_boundaries():
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                canceled(
                    fix_session_id="other",
                )
            ],
            request=request(),
        )
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                canceled(
                    fix_sequence=10,
                )
            ],
            request=request(),
        )


def test_rest_transport_evidence_requires_rest_request():
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                rest(
                    http_status=404,
                )
            ],
            request=request(),
        )
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                rest(
                    http_status=404,
                )
            ],
        )


def test_fix_cancel_reject_cannot_reconcile_rest_request():
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [reject()],
            request=request(
                transport=CancelTransport.REST,
            ),
        )


def test_terminal_provider_cancel_can_coexist_with_rejected_local_cancel_without_false_cause():
    p = reconcile_cancel(
        working(),
        [
            canceled(seq=11),
            reject(
                order_status=OrderStatus.CANCELED,
                seq=12,
            ),
        ],
        request=request(),
    )
    assert p.outcome is CancelOutcome.REJECTED
    assert p.provider_status is OrderStatus.CANCELED
    assert p.cause is CancelCause.PROVIDER_TERMINAL_UNSPECIFIED
    assert p.released_quantity == Decimal("10")
    assert p.requires_readback is False


def test_terminal_then_reject_with_non_canceled_status_is_conflict():
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                canceled(seq=11),
                reject(
                    order_status=OrderStatus.FILLED,
                    seq=12,
                ),
            ],
            request=request(),
        )


def test_fill_after_terminal_canceled_report_fails_closed():
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                canceled(seq=11),
                fill(seq=12),
            ],
            request=request(),
        )


def test_checkpoint_tamper_and_duplicate_json_fail():
    p = reconcile_cancel(
        working(),
        [],
        request=request(),
    )
    raw = encode_checkpoint(p)
    obj = json.loads(raw)
    obj["payload"]["released_quantity"] = "999"
    with pytest.raises(ProphetXCancelConflict):
        decode_checkpoint(
            json.dumps(obj)
        )
    with pytest.raises(ProphetXCancelConflict):
        decode_checkpoint(
            '{"schema_version":1,"schema_version":1,'
            '"authority":"x","payload":{},'
            '"payload_sha256":"x"}'
        )


def test_sandbox_evidence_never_claims_readiness_or_real_money_truth():
    p = reconcile_cancel(
        working(),
        [canceled()],
        request=request(),
    )
    assert not hasattr(
        p,
        "real_money_execution",
    )
    assert not hasattr(
        p,
        "readiness",
    )
    assert request().environment == "sandbox"


def test_rest_request_has_no_fix_cancel_clordid_and_scope_is_frozen():
    req = request(transport=CancelTransport.REST)
    assert req.cancel_cl_ord_id is None
    assert req.scope is CancelScope.WHOLE_REMAINDER
    with pytest.raises(ProphetXCancelError):
        request(
            transport=CancelTransport.REST,
            cancel_cl_ord_id="invented-fix-id",
        )


def test_cancel_reject_reason_is_closed_provider_contract():
    with pytest.raises(ProphetXCancelError):
        reject(reason="too late")
    assert (
        reject(reason=CancelRejectReason.UNKNOWN_ORDER).reason
        is CancelRejectReason.UNKNOWN_ORDER
    )


def test_rest_transport_state_is_closed_contract():
    with pytest.raises(ProphetXCancelError):
        rest(state="timeout")
    assert (
        rest(
            http_status=200,
            state=RestTransportState.HTTP_200_UNQUALIFIED,
        ).state
        is RestTransportState.HTTP_200_UNQUALIFIED
    )


def test_rest_transport_state_must_match_http_status():
    with pytest.raises(ProphetXCancelError):
        rest(
            http_status=200,
            state=RestTransportState.TIMEOUT_AFTER_POSSIBLE_SEND,
        )
    with pytest.raises(ProphetXCancelError):
        rest(
            http_status=404,
            state=RestTransportState.HTTP_200_UNQUALIFIED,
        )
    with pytest.raises(ProphetXCancelError):
        rest(
            http_status=200,
            state=RestTransportState.HTTP_404_SAMPLE_AMBIGUOUS,
        )


def test_transport_and_reject_response_cannot_predate_request():
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                rest(
                    observed_at="2026-09-22T20:00:01Z",
                )
            ],
            request=request(
                transport=CancelTransport.REST,
            ),
        )
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                reject(
                    transact_time="2026-09-22T20:00:01Z",
                )
            ],
            request=request(),
        )


def test_distinct_duplicate_transport_or_cancel_reject_is_fail_closed():
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                rest(observation_id="r1"),
                rest(observation_id="r2"),
            ],
            request=request(
                transport=CancelTransport.REST,
            ),
        )
    with pytest.raises(ProphetXCancelConflict):
        reconcile_cancel(
            working(),
            [
                reject(
                    reject_id="r1",
                    seq=11,
                ),
                reject(
                    reject_id="r2",
                    seq=12,
                ),
            ],
            request=request(),
        )


def test_working_order_state_invariants():
    with pytest.raises(ProphetXCancelError):
        working(
            cumulative_filled="1",
            leaves_quantity="8",
            average_fill_price="2",
        )
    with pytest.raises(ProphetXCancelError):
        working(
            cumulative_filled="1",
            leaves_quantity="9",
            average_fill_price=None,
        )
    with pytest.raises(ProphetXCancelError):
        working(
            status=OrderStatus.NEW,
            cumulative_filled="1",
            leaves_quantity="9",
            average_fill_price="2",
        )
    with pytest.raises(ProphetXCancelError):
        working(
            status=OrderStatus.PARTIALLY_FILLED,
            cumulative_filled="0",
            leaves_quantity="10",
            average_fill_price=None,
        )

def test_structural_cancel_projection_is_explicitly_non_authoritative():
    p = reconcile_cancel(
        working(),
        [canceled()],
        request=request(),
    )

    # The structural reducer may calculate the candidate release, but caller-shaped
    # DTOs are not canonical ProphetX provider-origin evidence.
    assert p.released_quantity == Decimal("10")
    assert p.provider_origin_authoritative is False
    assert p.exposure_release_authorized is False
    with pytest.raises(ProphetXCancelError, match="provider-origin authority"):
        p.assert_provider_origin_authoritative()

    wire = p.wire()
    assert wire["provider_origin_authority"] == "STRUCTURAL_ONLY_UNVERIFIED_PROVIDER_ORIGIN"
    assert wire["provider_origin_authoritative"] is False
    assert wire["exposure_release_authorized"] is False

    restored = decode_checkpoint(encode_checkpoint(p))
    assert restored.released_quantity == Decimal("10")
    assert restored.provider_origin_authoritative is False
    assert restored.exposure_release_authorized is False


def test_checkpoint_cannot_launder_structural_projection_into_provider_authority():
    import autosport.prophetx_cancel_reconciliation as cancel_module

    p = reconcile_cancel(
        working(),
        [canceled()],
        request=request(),
    )
    obj = json.loads(encode_checkpoint(p))
    obj["payload"]["provider_origin_authority"] = "PROVIDER_AUTHORITATIVE"
    obj["payload"]["provider_origin_authoritative"] = True
    obj["payload"]["exposure_release_authorized"] = True

    # Recompute the public deterministic cache digest to prove the digest itself is
    # not an authority boundary. The decoder must reject the semantic elevation.
    obj["payload_sha256"] = cancel_module._digest(obj["payload"])
    forged = cancel_module._canon(obj)
    with pytest.raises(ProphetXCancelConflict, match="checkpoint payload mismatch"):
        decode_checkpoint(forged)

