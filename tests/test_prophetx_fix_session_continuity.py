from datetime import timedelta

import pytest

from autosport.prophetx_fix_session_continuity import (
    ContinuityAction,
    ExecObservationRelation,
    FixStream,
    ProphetXFixExecutionObservation,
    ProphetXFixSequenceCheckpoint,
    ProphetXFixSessionIdentity,
    ResendAction,
    assess_logon_continuity,
    assess_resend_window,
    compare_execution_observations,
    compare_sequence_progress,
)


def identity(
    *,
    environment: str = "SANDBOX",
    sender: str = "AUTOSPORT",
    target: str = "PROPHETX",
    credential: str = "sha256:" + "a" * 64,
) -> ProphetXFixSessionIdentity:
    return ProphetXFixSessionIdentity(
        environment=environment,
        begin_string="FIX.4.4",
        sender_comp_id=sender,
        target_comp_id=target,
        credential_revision_fingerprint=credential,
    )


def checkpoint(
    *,
    stream: FixStream = FixStream.PRIMARY,
    expected: int = 11,
    last_durable: int = 10,
    reset_epoch: int = 3,
    session: ProphetXFixSessionIdentity | None = None,
) -> ProphetXFixSequenceCheckpoint:
    return ProphetXFixSequenceCheckpoint(
        identity=session or identity(),
        stream=stream,
        next_expected_inbound_seq_num=expected,
        next_outbound_seq_num=21,
        last_durable_inbound_seq_num=last_durable,
        reset_epoch=reset_epoch,
    )


def observation(
    *,
    stream: FixStream = FixStream.PRIMARY,
    session: ProphetXFixSessionIdentity | None = None,
    seq: int = 50,
    exec_id: str = "exec-1",
    order_id: str = "order-1",
    semantic: str = "sha256:fill-A",
    account: str = "acct-opaque-1",
) -> ProphetXFixExecutionObservation:
    return ProphetXFixExecutionObservation(
        environment="SANDBOX",
        economic_account_ref=account,
        session_identity=session or identity(),
        stream=stream,
        msg_seq_num=seq,
        exec_id=exec_id,
        provider_order_id=order_id,
        semantic_fingerprint=semantic,
    )


def test_persistent_session_identity_is_exact_and_secret_free_evidence() -> None:
    item = identity()
    assert item.persistent_session_key == "FIX.4.4:AUTOSPORT:PROPHETX"
    evidence = dict(item.to_evidence())
    assert evidence["credential_revision_fingerprint"] == "sha256:" + "a" * 64
    assert set(evidence) == {
        "environment",
        "begin_string",
        "sender_comp_id",
        "target_comp_id",
        "credential_revision_fingerprint",
        "persistent_session_key",
    }


def test_tcp_reconnect_with_equal_sequence_resumes_without_reset() -> None:
    result = assess_logon_continuity(checkpoint(), provider_logon_msg_seq_num=11)
    assert result.action is ContinuityAction.RESUME
    assert result.reset_epoch_after_action == 3
    assert not result.application_reconciliation_required
    assert not result.authorizes_economic_action


def test_provider_sequence_behind_requires_reset_and_application_reconciliation() -> None:
    result = assess_logon_continuity(checkpoint(), provider_logon_msg_seq_num=7)
    assert result.action is ContinuityAction.RESET_AND_RECONCILE
    assert result.reset_epoch_after_action == 4
    assert result.application_reconciliation_required


def test_provider_sequence_ahead_requests_resend_not_reset() -> None:
    result = assess_logon_continuity(checkpoint(), provider_logon_msg_seq_num=14)
    assert result.action is ContinuityAction.REQUEST_RESEND
    assert result.reset_epoch_after_action == 3
    assert not result.application_reconciliation_required


def test_lost_local_store_resets_transport_but_never_claims_application_completeness() -> None:
    result = assess_logon_continuity(
        checkpoint(),
        provider_logon_msg_seq_num=11,
        local_sequence_store_available=False,
    )
    assert result.action is ContinuityAction.RESET_AND_RECONCILE
    assert result.reason == "LOCAL_SEQUENCE_STATE_UNAVAILABLE"
    assert result.application_reconciliation_required
    assert not result.authorizes_economic_action


def test_missing_checkpoint_is_recovery_incident_not_fresh_positive_authority() -> None:
    result = assess_logon_continuity(None, provider_logon_msg_seq_num=1)
    assert result.action is ContinuityAction.RESET_AND_RECONCILE
    assert result.next_expected_inbound_seq_num is None
    assert result.application_reconciliation_required


def test_tag_789_is_ignored_and_cannot_change_resume_decision() -> None:
    result = assess_logon_continuity(
        checkpoint(),
        provider_logon_msg_seq_num=11,
        next_expected_msg_seq_num_tag=999_999,
    )
    assert result.action is ContinuityAction.RESUME
    assert result.next_expected_msg_seq_num_tag_ignored


def test_operator_reset_text_alone_does_not_rewrite_sequence_truth() -> None:
    result = assess_logon_continuity(
        checkpoint(),
        provider_logon_msg_seq_num=11,
        operator_reset_instruction_observed=True,
    )
    assert result.operator_reset_instruction_observed
    assert result.action is ContinuityAction.RESUME
    assert result.reset_epoch_after_action == 3


def test_resend_is_allowed_through_exact_seven_day_window() -> None:
    result = assess_resend_window(timedelta(days=7))
    assert result.action is ResendAction.REQUEST_RESEND
    assert result.resend_window_available
    assert not result.provider_history_complete
    assert not result.application_reconciliation_required
    assert not result.proves_omitted_application_events_absent


def test_disconnect_beyond_seven_days_requires_reset_and_reconciliation() -> None:
    result = assess_resend_window(timedelta(days=7, microseconds=1))
    assert result.action is ResendAction.RESET_AND_RECONCILE
    assert not result.resend_window_available
    assert not result.provider_history_complete
    assert result.application_reconciliation_required


def test_gapfill_pruning_never_proves_omitted_application_events_absent() -> None:
    result = assess_resend_window(
        timedelta(hours=2),
        provider_gap_fill_pruned=True,
    )
    assert result.action is ResendAction.PRUNED_GAP_RECONCILE
    assert not result.resend_window_available
    assert not result.provider_history_complete
    assert result.application_reconciliation_required
    assert not result.proves_omitted_application_events_absent


def test_primary_and_drop_copy_sequence_spaces_are_incomparable() -> None:
    primary = checkpoint(stream=FixStream.PRIMARY)
    drop_copy = checkpoint(stream=FixStream.DROP_COPY)
    with pytest.raises(ValueError, match="incomparable"):
        compare_sequence_progress(primary, drop_copy)


def test_different_persistent_session_identities_are_incomparable() -> None:
    first = checkpoint(session=identity(sender="AUTOSPORT-A"))
    second = checkpoint(session=identity(sender="AUTOSPORT-B"))
    with pytest.raises(ValueError, match="incomparable"):
        compare_sequence_progress(first, second)


def test_same_stream_and_session_sequence_progress_is_comparable() -> None:
    first = checkpoint(expected=11, last_durable=10)
    second = checkpoint(expected=12, last_durable=11)
    assert compare_sequence_progress(first, second) == -1
    assert compare_sequence_progress(second, first) == 1
    assert compare_sequence_progress(first, first) == 0


def test_same_exec_id_redelivered_on_drop_copy_is_one_equivalent_economic_observation() -> None:
    primary = observation(stream=FixStream.PRIMARY, seq=50)
    drop_copy = observation(
        stream=FixStream.DROP_COPY,
        session=identity(sender="AUTOSPORT-DROPCOPY"),
        seq=700,
    )
    assert (
        compare_execution_observations(primary, drop_copy)
        is ExecObservationRelation.IDEMPOTENT_DUPLICATE
    )


def test_same_exec_id_with_changed_economics_fails_closed_as_conflict() -> None:
    first = observation(semantic="sha256:fill-A")
    changed = observation(semantic="sha256:fill-B", seq=51)
    assert compare_execution_observations(first, changed) is ExecObservationRelation.CONFLICT


def test_same_exec_id_with_changed_provider_order_id_is_conflict() -> None:
    first = observation(order_id="order-1")
    changed = observation(order_id="order-2", seq=51)
    assert compare_execution_observations(first, changed) is ExecObservationRelation.CONFLICT


def test_same_exec_id_in_different_account_scope_is_distinct() -> None:
    first = observation(account="acct-A")
    other = observation(account="acct-B")
    assert compare_execution_observations(first, other) is ExecObservationRelation.DISTINCT


def test_execution_observation_environment_must_match_session_environment() -> None:
    with pytest.raises(ValueError, match="environment"):
        ProphetXFixExecutionObservation(
            environment="PRODUCTION",
            economic_account_ref="acct",
            session_identity=identity(environment="SANDBOX"),
            stream=FixStream.PRIMARY,
            msg_seq_num=1,
            exec_id="exec",
            provider_order_id="order",
            semantic_fingerprint="sha256:x",
        )


@pytest.mark.parametrize(
    ("value", "field"),
    [
        (0, "next_expected_inbound_seq_num"),
        (-1, "next_outbound_seq_num"),
    ],
)
def test_sequence_numbers_fail_closed(value: int, field: str) -> None:
    kwargs = dict(
        identity=identity(),
        stream=FixStream.PRIMARY,
        next_expected_inbound_seq_num=1,
        next_outbound_seq_num=1,
        last_durable_inbound_seq_num=0,
    )
    kwargs[field] = value
    with pytest.raises(ValueError):
        ProphetXFixSequenceCheckpoint(**kwargs)


def test_checkpoint_refuses_durable_sequence_ahead_of_next_expected() -> None:
    with pytest.raises(ValueError, match="last_durable"):
        checkpoint(expected=11, last_durable=11)


def test_negative_disconnect_duration_fails_closed() -> None:
    with pytest.raises(ValueError, match="negative"):
        assess_resend_window(timedelta(seconds=-1))


def test_credential_revision_must_be_non_secret_sha256_fingerprint() -> None:
    with pytest.raises(ValueError, match="sha256 fingerprint"):
        ProphetXFixSessionIdentity(
            environment="SANDBOX",
            begin_string="FIX.4.4",
            sender_comp_id="AUTOSPORT",
            target_comp_id="PROPHETX",
            credential_revision_fingerprint="raw-access-token",
        )


def test_environment_is_closed_to_sandbox_or_production() -> None:
    with pytest.raises(ValueError, match="SANDBOX or PRODUCTION"):
        identity(environment="STAGING")


def test_stream_must_be_typed_not_caller_string() -> None:
    with pytest.raises(TypeError, match="FixStream"):
        ProphetXFixSequenceCheckpoint(
            identity=identity(),
            stream="PRIMARY",  # type: ignore[arg-type]
            next_expected_inbound_seq_num=2,
            next_outbound_seq_num=2,
            last_durable_inbound_seq_num=1,
        )


def test_seven_day_plus_microsecond_evidence_does_not_truncate_boundary() -> None:
    result = assess_resend_window(timedelta(days=7, microseconds=1))
    assert result.disconnect_duration_microseconds == 604_800_000_001
    assert result.action is ResendAction.RESET_AND_RECONCILE
