from __future__ import annotations

import sqlite3

import pytest

from autosport.prophetx_fix_session_continuity import (
    FixReconnectPlan,
    FixSessionIdentity,
    ProphetXFixContinuityStore,
    ProphetXFixContractError,
    ProphetXFixEnvironment,
    ProphetXFixEvidenceConflict,
    ProphetXFixStream,
    ReconnectDisposition,
)

T0 = "2026-09-23T00:00:00+00:00"
T1 = "2026-09-23T00:01:00+00:00"
T2 = "2026-09-23T00:02:00+00:00"
T3 = "2026-09-23T00:03:00+00:00"


def _identity() -> FixSessionIdentity:
    return FixSessionIdentity(
        ProphetXFixEnvironment.SANDBOX,
        ProphetXFixStream.PRIMARY,
        "FIX.4.4",
        "PX-PRIMARY",
        "PROPHETX",
        "a" * 64,
    )


def test_caller_constructed_reset_plan_cannot_mint_durable_reset(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    checkpoint = store.initialize_session(identity, observed_at=T0)
    checkpoint = store.checkpoint_sequences(
        identity,
        expected_revision=checkpoint.revision,
        next_expected_inbound=42,
        next_outbound=17,
        observed_at=T1,
    )
    before = store.load_checkpoint(identity)

    legitimate = store.plan_reconnect(
        identity,
        provider_logon_msg_seq_num=42,
        observed_at=T2,
        disconnected_since=T1,
    )
    assert legitimate.disposition is ReconnectDisposition.RESUME
    assert legitimate.reset_seq_num_flag_candidate is False

    forged = FixReconnectPlan(
        identity,
        checkpoint.revision,
        ReconnectDisposition.RESET_PROVIDER_SEQUENCE_LOWER,
        1,
        True,
        None,
        None,
        True,
        False,
        T2,
    )

    with pytest.raises(
        ProphetXFixContractError,
        match="product-owned reconnect causal authority",
    ):
        store.record_reset(forged, observed_at=T3)

    assert store.load_checkpoint(identity) == before


def test_store_issued_reset_candidate_is_not_durable_reset_authority(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    checkpoint = store.initialize_session(identity, observed_at=T0)
    checkpoint = store.checkpoint_sequences(
        identity,
        expected_revision=checkpoint.revision,
        next_expected_inbound=42,
        next_outbound=17,
        observed_at=T1,
    )

    plan = store.plan_reconnect(
        identity,
        provider_logon_msg_seq_num=1,
        observed_at=T2,
        disconnected_since=T1,
    )
    assert plan.disposition is ReconnectDisposition.RESET_PROVIDER_SEQUENCE_LOWER
    with pytest.raises(
        ProphetXFixContractError,
        match="product-owned reconnect causal authority",
    ):
        store.record_reset(plan, observed_at=T3)
    assert store.load_checkpoint(identity) == checkpoint


def test_reset_candidate_stays_non_authoritative_across_restart(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    checkpoint = store.initialize_session(identity, observed_at=T0)
    checkpoint = store.checkpoint_sequences(
        identity,
        expected_revision=checkpoint.revision,
        next_expected_inbound=42,
        next_outbound=17,
        observed_at=T1,
    )
    plan = store.plan_reconnect(
        identity,
        provider_logon_msg_seq_num=1,
        observed_at=T2,
        disconnected_since=T1,
    )

    restarted = ProphetXFixContinuityStore(path)
    with pytest.raises(
        ProphetXFixContractError,
        match="product-owned reconnect causal authority",
    ):
        restarted.record_reset(plan, observed_at=T3)
    assert restarted.load_checkpoint(identity) == checkpoint


def test_newer_resume_facts_keep_old_reset_candidate_non_authoritative(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    checkpoint = store.initialize_session(identity, observed_at=T0)
    checkpoint = store.checkpoint_sequences(
        identity,
        expected_revision=checkpoint.revision,
        next_expected_inbound=42,
        next_outbound=17,
        observed_at=T1,
    )
    old_reset = store.plan_reconnect(
        identity,
        provider_logon_msg_seq_num=1,
        observed_at=T2,
        disconnected_since=T1,
    )
    resumed = store.plan_reconnect(
        identity,
        provider_logon_msg_seq_num=42,
        observed_at=T3,
        disconnected_since=T2,
    )
    assert resumed.disposition is ReconnectDisposition.RESUME

    with pytest.raises(
        ProphetXFixContractError,
        match="product-owned reconnect causal authority",
    ):
        store.record_reset(old_reset, observed_at=T3)
    assert store.load_checkpoint(identity) == checkpoint


def test_forged_elapsed_time_cannot_reset_durable_sequence_state(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    checkpoint = store.initialize_session(
        identity,
        next_expected_inbound=42,
        next_outbound=17,
        observed_at="2026-09-15T00:00:00+00:00",
    )
    forged = store.plan_reconnect(
        identity,
        provider_logon_msg_seq_num=42,
        observed_at="2026-09-23T00:00:01+00:00",
        disconnected_since="2026-09-15T00:00:00+00:00",
    )
    assert forged.disposition is ReconnectDisposition.RESET_RESEND_WINDOW_EXCEEDED
    assert forged.reset_seq_num_flag_candidate is True

    with pytest.raises(
        ProphetXFixContractError,
        match="product-owned reconnect causal authority",
    ):
        store.record_reset(forged, observed_at="2026-09-23T00:00:02+00:00")
    assert store.load_checkpoint(identity) == checkpoint


def test_injected_reset_authority_row_fails_closed_on_restart(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    checkpoint = store.initialize_session(identity, observed_at=T0)

    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO reset_plan_authority(
                session_key, checkpoint_revision, plan_sha256, observed_at
            ) VALUES (?, ?, ?, ?)""",
            (identity.session_key, checkpoint.revision, "f" * 64, T1),
        )
        conn.commit()

    with pytest.raises(
        ProphetXFixEvidenceConflict,
        match="reset plan authority|causal authority",
    ):
        ProphetXFixContinuityStore(path)
