from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3

import pytest

from autosport.betfair_heartbeat_safety import (
    BetfairHeartbeatSafetyError,
    HeartbeatAck,
    HeartbeatAction,
    HeartbeatScope,
    HeartbeatStore,
    evaluate,
)

BASE = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def ts(seconds=0):
    return (BASE + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def scope(account="acct"):
    return HeartbeatScope(account, sha("session"), sha("app"), "UK")


def ack(**kwargs):
    values = dict(
        scope=scope(),
        requested_timeout_seconds=30,
        actual_timeout_seconds=30,
        action=HeartbeatAction.NONE,
        received_at=ts(),
        received_monotonic_ns=10_000_000_000,
        response_sha256=sha("response"),
    )
    values.update(kwargs)
    return HeartbeatAck(**values)


def test_actual_timeout_drives_deadline_and_never_authorizes_execution():
    status = evaluate(
        scope(),
        ack(requested_timeout_seconds=300, actual_timeout_seconds=30),
        now_at=ts(19),
        now_monotonic_ns=29_000_000_000,
    )
    assert status.heartbeat_precondition
    assert status.next_due_at == ts(20)
    assert status.next_due_monotonic_ns == 30_000_000_000
    assert not status.provider_write_authorized
    assert not status.execution_admission_authorized
    assert not status.real_money_authorized


def test_margin_exhaustion_freezes_before_provider_expiry():
    status = evaluate(
        scope(),
        ack(),
        now_at=ts(21),
        now_monotonic_ns=31_000_000_000,
    )
    assert not status.heartbeat_precondition
    assert status.requires_reconciliation
    assert status.reason == "SAFETY_MARGIN_EXHAUSTED"


@pytest.mark.parametrize(
    "action",
    [value for value in HeartbeatAction if value is not HeartbeatAction.NONE],
)
def test_every_deadman_action_requires_reconciliation(action):
    status = evaluate(
        scope(),
        ack(action=action),
        now_at=ts(1),
        now_monotonic_ns=11_000_000_000,
    )
    assert not status.heartbeat_precondition
    assert status.requires_reconciliation
    assert status.reason == f"DEADMAN_ACTION_{action.value}"


def test_unregister_is_not_shutdown_safety_proof():
    status = evaluate(
        scope(),
        ack(requested_timeout_seconds=0, actual_timeout_seconds=0),
        now_at=ts(1),
        now_monotonic_ns=11_000_000_000,
    )
    assert not status.heartbeat_precondition
    assert status.reason == "UNREGISTERED"


def test_reboot_monotonic_regression_requires_fresh_ack():
    status = evaluate(
        scope(),
        ack(received_monotonic_ns=50_000_000_000),
        now_at=ts(1),
        now_monotonic_ns=1_000_000_000,
    )
    assert not status.heartbeat_precondition
    assert status.reason == "MONOTONIC_EPOCH_CHANGED"


def test_stream_liveness_cannot_substitute_for_deadman_ack():
    status = evaluate(
        scope(),
        None,
        now_at=ts(),
        now_monotonic_ns=10_000_000_000,
    )
    assert status.reason == "NO_ACK"
    assert not status.heartbeat_precondition


def test_restart_idempotency_and_integrity(tmp_path):
    store = HeartbeatStore(tmp_path, scope=scope())
    first = ack()
    assert store.record(first) == store.record(first)

    second = ack(
        received_at=ts(5),
        received_monotonic_ns=15_000_000_000,
        response_sha256=sha("second"),
    )
    store.record(second)

    reopened = HeartbeatStore(tmp_path, scope=scope())
    assert reopened.verify_integrity() == 2
    assert reopened.latest() == second
    assert reopened.evaluate(
        now_at=ts(6),
        now_monotonic_ns=16_000_000_000,
    ).heartbeat_precondition


def test_store_rejects_scope_rebind(tmp_path):
    HeartbeatStore(tmp_path, scope=scope())
    with pytest.raises(BetfairHeartbeatSafetyError, match="scope"):
        HeartbeatStore(tmp_path, scope=scope("other"))


def test_store_detects_payload_tamper(tmp_path):
    store = HeartbeatStore(tmp_path, scope=scope())
    store.record(ack())
    with sqlite3.connect(store.path) as db:
        raw = db.execute("SELECT payload FROM ack").fetchone()[0]
        db.execute(
            "UPDATE ack SET payload=?",
            (raw.replace('"actual":30', '"actual":31'),),
        )

    with pytest.raises(BetfairHeartbeatSafetyError):
        HeartbeatStore(tmp_path, scope=scope()).verify_integrity()


def test_noncanonical_time_and_registered_zero_timeout_fail():
    with pytest.raises(BetfairHeartbeatSafetyError):
        HeartbeatAck(
            scope(),
            30,
            30,
            HeartbeatAction.NONE,
            "2026-09-21T10:00:00+00:00",
            10,
            sha("x"),
        )
    with pytest.raises(BetfairHeartbeatSafetyError):
        ack(actual_timeout_seconds=0)
