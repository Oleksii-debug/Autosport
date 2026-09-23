from __future__ import annotations

import pytest

from autosport.prophetx_fix_session_continuity import (
    FixReconnectPlan,
    FixSessionIdentity,
    ProphetXFixContinuityStore,
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

    with pytest.raises(ProphetXFixEvidenceConflict):
        store.record_reset(forged, observed_at=T3)

    assert store.load_checkpoint(identity) == before


def test_store_issued_reset_plan_remains_one_shot_and_stale_safe(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    checkpoint = store.initialize_session(identity, observed_at=T0)
    store.checkpoint_sequences(
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
    reset = store.record_reset(plan, observed_at=T3)
    assert reset.reset_epoch == 1
    assert reset.application_reconciliation_required is True

    with pytest.raises(ProphetXFixEvidenceConflict, match="stale"):
        store.record_reset(plan, observed_at=T3)


def test_store_issued_reset_authority_survives_restart_until_consumed(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    checkpoint = store.initialize_session(identity, observed_at=T0)
    store.checkpoint_sequences(
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
    reset = restarted.record_reset(plan, observed_at=T3)
    assert reset.reset_epoch == 1
    assert reset.application_reconciliation_required is True


def test_newer_resume_facts_revoke_unconsumed_reset_authority(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    checkpoint = store.initialize_session(identity, observed_at=T0)
    store.checkpoint_sequences(
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

    with pytest.raises(ProphetXFixEvidenceConflict, match="issued"):
        store.record_reset(old_reset, observed_at=T3)
