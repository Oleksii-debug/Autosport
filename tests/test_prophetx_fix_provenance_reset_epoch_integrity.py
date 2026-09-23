from __future__ import annotations

import sqlite3

import pytest

from autosport.prophetx_fix_session_continuity import (
    ExecutionReportObservation,
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
T4 = "2026-09-23T00:04:00+00:00"


def _identity() -> FixSessionIdentity:
    return FixSessionIdentity(
        ProphetXFixEnvironment.SANDBOX,
        ProphetXFixStream.PRIMARY,
        "FIX.4.4",
        "PX-PRIMARY",
        "PROPHETX",
        "a" * 64,
    )


def _record_one(store: ProphetXFixContinuityStore, identity: FixSessionIdentity) -> None:
    store.record_execution_report(
        ExecutionReportObservation(
            identity,
            1,
            "exec-epoch-1",
            "client-order-1",
            "provider-order-1",
            T1,
            "b" * 64,
            T2,
        )
    )


def test_restart_rejects_provenance_rebound_into_future_reset_epoch(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    store.initialize_session(identity, observed_at=T0)
    _record_one(store, identity)

    with sqlite3.connect(path) as conn:
        changed = conn.execute(
            "UPDATE report_provenance SET reset_epoch = reset_epoch + 1"
        ).rowcount
        assert changed == 1
        conn.commit()

    with pytest.raises(
        ProphetXFixEvidenceConflict,
        match="reset epoch|future reset|provenance",
    ):
        ProphetXFixContinuityStore(path)


def test_restart_keeps_legitimate_prior_epoch_provenance_after_reset(tmp_path) -> None:
    path = tmp_path / "fix-continuity.sqlite3"
    store = ProphetXFixContinuityStore(path)
    identity = _identity()
    checkpoint = store.initialize_session(identity, observed_at=T0)
    _record_one(store, identity)

    checkpoint = store.checkpoint_sequences(
        identity,
        expected_revision=checkpoint.revision,
        next_expected_inbound=10,
        next_outbound=2,
        observed_at=T2,
    )
    plan = store.plan_reconnect(
        identity,
        provider_logon_msg_seq_num=1,
        observed_at=T3,
        disconnected_since=T2,
    )
    assert plan.disposition is ReconnectDisposition.RESET_PROVIDER_SEQUENCE_LOWER
    reset = store.record_reset(plan, observed_at=T4)
    assert reset.reset_epoch == 1

    restarted = ProphetXFixContinuityStore(path)
    assert restarted.load_checkpoint(identity).reset_epoch == 1
