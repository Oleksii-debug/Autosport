from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile

import pytest

from autosport.prophetx_fix_session_continuity import (
    ExecutionReportDisposition,
    ExecutionReportObservation,
    FixSessionIdentity,
    ProphetXFixContinuityStore,
    ProphetXFixContractError,
    ProphetXFixCheckpointMissing,
    ProphetXFixEnvironment,
    ProphetXFixEvidenceConflict,
    ProphetXFixStream,
    ReconnectDisposition,
)

T0 = "2026-09-23T00:00:00+00:00"
T1 = "2026-09-23T00:01:00+00:00"
T2 = "2026-09-23T00:02:00+00:00"
DAY8 = "2026-09-23T00:00:01+00:00"
DAY8_START = "2026-09-15T00:00:00+00:00"


def ident(
    stream: ProphetXFixStream = ProphetXFixStream.PRIMARY,
    *,
    env: ProphetXFixEnvironment = ProphetXFixEnvironment.SANDBOX,
    sender: str | None = None,
) -> FixSessionIdentity:
    if sender is None:
        sender = "PX-PRIMARY" if stream is ProphetXFixStream.PRIMARY else "PX-DROPCOPY"
    return FixSessionIdentity(
        env,
        stream,
        "FIX.4.4",
        sender,
        "PROPHETX",
        "a" * 64,
    )


def report(
    identity: FixSessionIdentity,
    *,
    seq: int,
    exec_id: str = "exec-1",
    payload: str = "b" * 64,
    observed: str = T2,
) -> ExecutionReportObservation:
    return ExecutionReportObservation(
        identity,
        seq,
        exec_id,
        "client-order-1",
        "provider-order-1",
        T1,
        payload,
        observed,
    )


def test_restart_keeps_sequence_state_and_does_not_daily_reset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fix-continuity.sqlite3"
        store = ProphetXFixContinuityStore(path)
        identity = ident()
        cp = store.initialize_session(identity, observed_at=T0)
        cp = store.checkpoint_sequences(
            identity,
            expected_revision=cp.revision,
            next_expected_inbound=42,
            next_outbound=17,
            observed_at=T1,
        )
        restarted = ProphetXFixContinuityStore(path)
        loaded = restarted.load_checkpoint(identity)
        assert loaded == cp
        plan = restarted.plan_reconnect(
            identity,
            provider_logon_msg_seq_num=42,
            observed_at=T2,
            disconnected_since="2026-09-22T23:59:00+00:00",
        )
        assert plan.disposition is ReconnectDisposition.RESUME
        assert plan.reset_seq_num_flag_candidate is False
        assert plan.provider_write_authorized is False
        assert plan.execution_authorized is False


def test_provider_lower_sequence_is_reset_candidate_and_requires_reconciliation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        cp = store.initialize_session(identity, observed_at=T0)
        store.checkpoint_sequences(
            identity,
            expected_revision=cp.revision,
            next_expected_inbound=50,
            next_outbound=25,
            observed_at=T1,
        )
        plan = store.plan_reconnect(
            identity,
            provider_logon_msg_seq_num=10,
            observed_at=T2,
            disconnected_since=T1,
        )
        assert plan.disposition is ReconnectDisposition.RESET_PROVIDER_SEQUENCE_LOWER
        assert plan.reset_seq_num_flag_candidate is True
        assert plan.application_reconciliation_required is True
        reset = store.record_reset(plan, observed_at="2026-09-23T00:03:00+00:00")
        assert reset.next_expected_inbound == 1
        assert reset.next_outbound == 1
        assert reset.reset_epoch == 1
        assert reset.application_reconciliation_required is True


def test_lost_local_store_is_explicit_reset_incident_not_completeness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        plan = store.plan_reconnect(
            identity,
            provider_logon_msg_seq_num=1,
            observed_at=T1,
            disconnected_since=None,
            local_sequence_store_lost=True,
        )
        assert plan.disposition is ReconnectDisposition.RESET_LOCAL_STORE_LOST
        assert plan.economic_state_complete is False
        reset = store.record_reset(plan, observed_at=T2)
        assert reset.reset_epoch == 1
        assert reset.application_reconciliation_required is True


def test_higher_provider_sequence_requests_gap_with_exact_range() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        cp = store.initialize_session(identity, observed_at=T0)
        store.checkpoint_sequences(
            identity,
            expected_revision=cp.revision,
            next_expected_inbound=12,
            next_outbound=9,
            observed_at=T1,
        )
        plan = store.plan_reconnect(
            identity,
            provider_logon_msg_seq_num=17,
            observed_at=T2,
            disconnected_since=T1,
        )
        assert plan.disposition is ReconnectDisposition.RESEND_REQUIRED
        assert (plan.resend_begin_seq, plan.resend_end_seq) == (12, 16)
        assert plan.reset_seq_num_flag_candidate is False
        assert plan.next_expected_msg_seq_num_789_authoritative is False


def test_venue_reset_text_does_not_override_sequence_facts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        store.initialize_session(identity, next_expected_inbound=7, observed_at=T0)
        plan = store.plan_reconnect(
            identity,
            provider_logon_msg_seq_num=7,
            observed_at=T1,
            disconnected_since=T0,
            venue_reset_notice_seen=True,
        )
        assert plan.venue_reset_notice_seen is True
        assert plan.disposition is ReconnectDisposition.RESUME
        assert plan.reset_seq_num_flag_candidate is False


def test_disconnect_over_seven_days_forces_reset_and_reconciliation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        store.initialize_session(identity, next_expected_inbound=77, observed_at=DAY8_START)
        plan = store.plan_reconnect(
            identity,
            provider_logon_msg_seq_num=77,
            observed_at=DAY8,
            disconnected_since=DAY8_START,
        )
        assert plan.disposition is ReconnectDisposition.RESET_RESEND_WINDOW_EXCEEDED
        assert plan.reset_seq_num_flag_candidate is True
        assert plan.application_reconciliation_required is True


def test_exactly_seven_days_does_not_force_pruned_window_reset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        start = "2026-09-16T00:00:00+00:00"
        end = "2026-09-23T00:00:00+00:00"
        store.initialize_session(identity, next_expected_inbound=9, observed_at=start)
        plan = store.plan_reconnect(
            identity, provider_logon_msg_seq_num=9, observed_at=end, disconnected_since=start
        )
        assert plan.disposition is ReconnectDisposition.RESUME
        assert plan.reset_seq_num_flag_candidate is False


def test_pruned_gap_marks_application_reconciliation_required_durably() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.sqlite3"
        store = ProphetXFixContinuityStore(path)
        identity = ident()
        store.initialize_session(identity, next_expected_inbound=20, observed_at=T0)
        cp = store.record_pruned_gap(
            identity,
            begin_seq=10,
            end_seq=19,
            evidence_sha256="c" * 64,
            recorded_at=T1,
        )
        assert cp.application_reconciliation_required is True
        restarted = ProphetXFixContinuityStore(path)
        assert restarted.load_checkpoint(identity).application_reconciliation_required is True
        same = restarted.record_pruned_gap(
            identity,
            begin_seq=10,
            end_seq=19,
            evidence_sha256="c" * 64,
            recorded_at=T2,
        )
        assert same.application_reconciliation_required is True
        with pytest.raises(ProphetXFixEvidenceConflict):
            restarted.record_pruned_gap(
                identity,
                begin_seq=10,
                end_seq=19,
                evidence_sha256="d" * 64,
                recorded_at=T2,
            )


def test_reconciliation_flag_cannot_be_cleared_by_caller_digest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        store.initialize_session(identity, observed_at=T0)
        cp = store.record_pruned_gap(
            identity,
            begin_seq=1,
            end_seq=3,
            evidence_sha256="c" * 64,
            recorded_at=T1,
        )
        with pytest.raises(ProphetXFixContractError, match="product-owned canonical resolver"):
            store.clear_application_reconciliation_requirement(
                identity,
                expected_revision=cp.revision,
                canonical_reconciliation_evidence_sha256="e" * 64,
                observed_at=T2,
            )
        assert store.load_checkpoint(identity).application_reconciliation_required is True
        assert store.load_checkpoint(identity).application_completeness_proven is False


def test_same_execid_redelivery_is_idempotent_across_restart() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.sqlite3"
        store = ProphetXFixContinuityStore(path)
        identity = ident()
        store.initialize_session(identity, observed_at=T0)
        first = store.record_execution_report(report(identity, seq=10))
        assert first.disposition is ExecutionReportDisposition.FIRST_SEEN
        assert first.provenance_observation_count == 1
        restarted = ProphetXFixContinuityStore(path)
        duplicate = restarted.record_execution_report(report(identity, seq=10))
        assert duplicate.disposition is ExecutionReportDisposition.DUPLICATE_SAME_STREAM
        assert duplicate.provenance_observation_count == 1
        assert duplicate.economic_application_authorized is False


def test_same_msgseq_replay_at_later_local_observation_time_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        store.initialize_session(identity, observed_at=T0)
        store.record_execution_report(report(identity, seq=10, observed=T1))
        replay = store.record_execution_report(report(identity, seq=10, observed=T2))
        assert replay.disposition is ExecutionReportDisposition.DUPLICATE_SAME_STREAM
        assert replay.provenance_observation_count == 1


def test_same_execid_different_sequence_same_economics_is_duplicate_not_second_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        store.initialize_session(identity, observed_at=T0)
        store.record_execution_report(report(identity, seq=10))
        duplicate = store.record_execution_report(report(identity, seq=11))
        assert duplicate.disposition is ExecutionReportDisposition.DUPLICATE_SAME_STREAM
        assert duplicate.provenance_observation_count == 2
        assert duplicate.distinct_stream_count == 1


def test_same_execid_conflicting_economics_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        store.initialize_session(identity, observed_at=T0)
        store.record_execution_report(report(identity, seq=10, payload="b" * 64))
        with pytest.raises(ProphetXFixEvidenceConflict, match="ExecID"):
            store.record_execution_report(report(identity, seq=11, payload="f" * 64))


def test_same_msgseq_conflicting_execid_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        store.initialize_session(identity, observed_at=T0)
        store.record_execution_report(report(identity, seq=10, exec_id="exec-1"))
        with pytest.raises(ProphetXFixEvidenceConflict, match="MsgSeqNum"):
            store.record_execution_report(report(identity, seq=10, exec_id="exec-2"))


def test_primary_and_dropcopy_have_independent_sequences_but_one_execid_event() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        primary = ident(ProphetXFixStream.PRIMARY)
        drop = replace(
            ident(ProphetXFixStream.DROP_COPY),
            credential_identity_sha256="f" * 64,
        )
        store.initialize_session(primary, next_expected_inbound=101, observed_at=T0)
        store.initialize_session(drop, next_expected_inbound=7, observed_at=T0)
        first = store.record_execution_report(report(primary, seq=100))
        mirrored = store.record_execution_report(report(drop, seq=6))
        assert first.disposition is ExecutionReportDisposition.FIRST_SEEN
        assert mirrored.disposition is ExecutionReportDisposition.DUPLICATE_CROSS_STREAM
        assert mirrored.provenance_observation_count == 2
        assert mirrored.distinct_stream_count == 2
        assert store.load_checkpoint(primary).next_expected_inbound == 101
        assert store.load_checkpoint(drop).next_expected_inbound == 7


def test_sandbox_and_production_execid_do_not_alias() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        sandbox = ident(env=ProphetXFixEnvironment.SANDBOX)
        production = ident(env=ProphetXFixEnvironment.PRODUCTION, sender="PX-PROD")
        store.initialize_session(sandbox, observed_at=T0)
        store.initialize_session(production, observed_at=T0)
        first = store.record_execution_report(report(sandbox, seq=1))
        prod = store.record_execution_report(report(production, seq=1))
        assert first.disposition is ExecutionReportDisposition.FIRST_SEEN
        assert prod.disposition is ExecutionReportDisposition.FIRST_SEEN
        assert first.economic_event_key != prod.economic_event_key


def test_credential_identity_change_cannot_inherit_or_mint_reset_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        old = ident()
        store.initialize_session(old, next_expected_inbound=50, observed_at=T0)
        new = replace(old, credential_identity_sha256="f" * 64)
        with pytest.raises(ProphetXFixCheckpointMissing):
            store.plan_reconnect(
                new,
                provider_logon_msg_seq_num=50,
                observed_at=T1,
                disconnected_since=T0,
            )
        assert store.load_checkpoint(old).next_expected_inbound == 50


def test_stale_checkpoint_revision_cannot_overwrite_newer_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        cp = store.initialize_session(identity, observed_at=T0)
        newer = store.checkpoint_sequences(
            identity,
            expected_revision=cp.revision,
            next_expected_inbound=2,
            next_outbound=2,
            observed_at=T1,
        )
        assert newer.revision == cp.revision + 1
        with pytest.raises(ProphetXFixEvidenceConflict, match="stale"):
            store.checkpoint_sequences(
                identity,
                expected_revision=cp.revision,
                next_expected_inbound=3,
                next_outbound=3,
                observed_at=T2,
            )


def test_sequence_rollback_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ProphetXFixContinuityStore(Path(tmp) / "state.sqlite3")
        identity = ident()
        cp = store.initialize_session(
            identity, next_expected_inbound=10, next_outbound=10, observed_at=T0
        )
        with pytest.raises(ProphetXFixEvidenceConflict, match="rollback"):
            store.checkpoint_sequences(
                identity,
                expected_revision=cp.revision,
                next_expected_inbound=9,
                next_outbound=10,
                observed_at=T1,
            )


def test_bool_sequence_and_raw_secret_shaped_fields_reject() -> None:
    with pytest.raises(ProphetXFixContractError):
        ExecutionReportObservation(
            ident(),
            True,
            "exec",
            "client",
            "provider",
            T0,
            "b" * 64,
            T1,
        )
    with pytest.raises(ProphetXFixContractError):
        FixSessionIdentity(
            ProphetXFixEnvironment.SANDBOX,
            ProphetXFixStream.PRIMARY,
            "FIX.4.4",
            "user\npassword=secret",
            "PROPHETX",
            "a" * 64,
        )


def test_future_transact_time_rejects() -> None:
    with pytest.raises(ProphetXFixContractError, match="future"):
        ExecutionReportObservation(
            ident(),
            1,
            "exec",
            "client",
            "provider",
            "2026-09-23T00:03:00+00:00",
            "b" * 64,
            T2,
        )


def test_database_tamper_is_detected_on_integrity_verification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.sqlite3"
        store = ProphetXFixContinuityStore(path)
        identity = ident()
        store.initialize_session(identity, observed_at=T0)
        store.record_execution_report(report(identity, seq=1))
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE execution_events SET economic_payload_sha256=?", ("f" * 64,))
            conn.commit()
        with pytest.raises(ProphetXFixEvidenceConflict, match="semantic digest"):
            store.verify_integrity()


def test_corrupt_checkpoint_is_not_laundered_into_local_store_lost_reset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.sqlite3"
        store = ProphetXFixContinuityStore(path)
        identity = ident()
        store.initialize_session(identity, observed_at=T0)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                "UPDATE sessions SET identity_json=? WHERE session_key=?",
                ('{"broken":true}', identity.session_key),
            )
            conn.commit()
        with pytest.raises(ProphetXFixContractError, match="malformed"):
            store.plan_reconnect(
                identity,
                provider_logon_msg_seq_num=1,
                observed_at=T1,
                disconnected_since=T0,
            )


def test_store_rejects_unknown_schema_version() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.sqlite3"
        store = ProphetXFixContinuityStore(path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
            conn.commit()
        with pytest.raises(ProphetXFixContractError, match="schema version"):
            ProphetXFixContinuityStore(path)
