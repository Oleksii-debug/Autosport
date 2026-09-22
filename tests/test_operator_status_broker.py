from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from autosport.operator_status_broker import (
    MAX_MESSAGE_CHARS,
    AnnouncementPriority,
    OperatorStatusAnnouncementBroker,
    OperatorStatusConflictError,
    OperatorStatusIntegrityError,
    OperatorStatusNotPendingError,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


def store(tmp_path, clock=None):
    return OperatorStatusAnnouncementBroker(
        tmp_path / "дані з пробілами" / "operator-status.sqlite3",
        clock=clock or Clock(),
    )


def test_assertive_survives_restart_until_ack(tmp_path):
    clock = Clock()
    broker = store(tmp_path, clock)
    record = broker.publish(
        idempotency_key="recovery/1",
        priority=AnnouncementPriority.ASSERTIVE,
        message="Потрібне відновлення.",
    )
    broker.close()
    reopened = store(tmp_path, clock)
    assert [x.event_id for x in reopened.pending()] == [record.event_id]
    presentation = reopened.pending_presentations()[0]
    assert (presentation.live_mode, presentation.role) == ("assertive", "alert")
    assert presentation.request_focus is False
    assert presentation.focus_target is None
    acked = reopened.acknowledge(record.event_id)
    assert acked.acknowledged_at is not None
    reopened.close()
    assert store(tmp_path, clock).pending() == ()


def test_polite_latest_per_key_coalesces_without_focus(tmp_path):
    broker = store(tmp_path)
    first = broker.publish(
        idempotency_key="progress/1",
        priority=AnnouncementPriority.POLITE,
        coalesce_key="progress",
        message="10%",
    )
    latest = broker.publish(
        idempotency_key="progress/2",
        priority=AnnouncementPriority.POLITE,
        coalesce_key="progress",
        message="20%",
    )
    other = broker.publish(
        idempotency_key="other/1",
        priority=AnnouncementPriority.POLITE,
        coalesce_key="other",
        message="Готово.",
    )
    assert [x.event_id for x in broker.pending()] == [latest.event_id, other.event_id]
    with pytest.raises(OperatorStatusNotPendingError):
        broker.acknowledge(first.event_id)
    presentation = broker.presentation_for(latest)
    assert (presentation.live_mode, presentation.role) == ("polite", "status")
    assert not presentation.request_focus


def test_assertive_events_are_not_coalesced(tmp_path):
    broker = store(tmp_path)
    one = broker.publish(idempotency_key="a/1", priority=AnnouncementPriority.ASSERTIVE, message="A")
    two = broker.publish(idempotency_key="a/2", priority=AnnouncementPriority.ASSERTIVE, message="B")
    assert [x.event_id for x in broker.pending()] == [one.event_id, two.event_id]


def test_silent_is_durable_but_not_deliverable(tmp_path):
    broker = store(tmp_path)
    record = broker.publish(
        idempotency_key="silent/1",
        priority=AnnouncementPriority.SILENT,
        message="Внутрішній запис.",
    )
    assert broker.pending() == ()
    with pytest.raises(ValueError):
        broker.presentation_for(record)
    with pytest.raises(OperatorStatusNotPendingError):
        broker.acknowledge(record.event_id)


def test_exact_replay_is_idempotent_and_conflict_fails_closed(tmp_path):
    broker = store(tmp_path)
    first = broker.publish(
        idempotency_key="dataset/ready",
        priority=AnnouncementPriority.POLITE,
        coalesce_key="dataset",
        message="Готово.",
    )
    replay = broker.publish(
        idempotency_key="dataset/ready",
        priority=AnnouncementPriority.POLITE,
        coalesce_key="dataset",
        message="Готово.",
    )
    assert replay == first
    with pytest.raises(OperatorStatusConflictError):
        broker.publish(
            idempotency_key="dataset/ready",
            priority=AnnouncementPriority.POLITE,
            coalesce_key="dataset",
            message="Інше.",
        )


def test_explicit_occurrence_time_is_bound(tmp_path):
    clock = Clock()
    occurred = datetime(2026, 9, 22, 19, 30, tzinfo=timezone.utc)
    broker = store(tmp_path, clock)
    record = broker.publish(
        idempotency_key="event/1",
        priority=AnnouncementPriority.ASSERTIVE,
        message="Подія.",
        occurred_at=occurred,
    )
    assert record.occurred_at == occurred
    with pytest.raises(OperatorStatusConflictError):
        broker.publish(
            idempotency_key="event/1",
            priority=AnnouncementPriority.ASSERTIVE,
            message="Подія.",
            occurred_at=occurred - timedelta(seconds=1),
        )


def test_future_occurrence_rejected(tmp_path):
    clock = Clock()
    broker = store(tmp_path, clock)
    with pytest.raises(ValueError):
        broker.publish(
            idempotency_key="future",
            priority=AnnouncementPriority.ASSERTIVE,
            message="Майбутнє.",
            occurred_at=datetime(2026, 9, 22, 21, 0, tzinfo=timezone.utc),
        )


def test_contract_input_bounds_are_strict(tmp_path):
    broker = store(tmp_path)
    bad = [
        dict(idempotency_key=" bad ", priority=AnnouncementPriority.ASSERTIVE, message="x"),
        dict(idempotency_key="x", priority="ASSERTIVE", message="x"),
        dict(idempotency_key="x", priority=AnnouncementPriority.POLITE, message="x"),
        dict(idempotency_key="x", priority=AnnouncementPriority.ASSERTIVE, message="x", coalesce_key="no"),
        dict(idempotency_key="x", priority=AnnouncementPriority.ASSERTIVE, message="x" * (MAX_MESSAGE_CHARS + 1)),
    ]
    for kwargs in bad:
        with pytest.raises(ValueError):
            broker.publish(**kwargs)


def test_pending_limit_is_strict_and_ordered(tmp_path):
    broker = store(tmp_path)
    ids = [
        broker.publish(
            idempotency_key=f"a/{i}",
            priority=AnnouncementPriority.ASSERTIVE,
            message=str(i),
        ).event_id
        for i in range(3)
    ]
    assert [x.event_id for x in broker.pending(limit=2)] == ids[:2]
    for bad in (0, 1001, True, 1.5):
        with pytest.raises(ValueError):
            broker.pending(limit=bad)


def test_ack_is_idempotent_across_restart(tmp_path):
    clock = Clock()
    broker = store(tmp_path, clock)
    record = broker.publish(
        idempotency_key="ack/1",
        priority=AnnouncementPriority.ASSERTIVE,
        message="Увага.",
    )
    first = broker.acknowledge(record.event_id)
    second = broker.acknowledge(record.event_id)
    assert second == first
    broker.close()
    third = store(tmp_path, clock).acknowledge(record.event_id)
    assert third == first


def test_unknown_ack_fails_closed(tmp_path):
    with pytest.raises(OperatorStatusNotPendingError):
        store(tmp_path).acknowledge("a" * 64)


def test_tamper_is_rejected_on_restart(tmp_path):
    path = tmp_path / "status.sqlite3"
    clock = Clock()
    broker = OperatorStatusAnnouncementBroker(path, clock=clock)
    broker.publish(idempotency_key="t/1", priority=AnnouncementPriority.ASSERTIVE, message="Оригінал.")
    broker.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE events SET message='Підміна.'")
        db.commit()
    with pytest.raises(OperatorStatusIntegrityError):
        OperatorStatusAnnouncementBroker(path, clock=clock)


def test_invalid_schema_is_rejected(tmp_path):
    path = tmp_path / "status.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("INSERT INTO metadata VALUES('schema_version','999')")
        db.commit()
    with pytest.raises(OperatorStatusIntegrityError):
        OperatorStatusAnnouncementBroker(path)


def test_nonfinite_timeout_rejected(tmp_path):
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite positive"):
            OperatorStatusAnnouncementBroker(tmp_path / "x.db", sqlite_timeout_seconds=value)


def test_two_instances_share_monotonic_sequence(tmp_path):
    path = tmp_path / "status.sqlite3"
    clock = Clock()
    one = OperatorStatusAnnouncementBroker(path, clock=clock)
    two = OperatorStatusAnnouncementBroker(path, clock=clock)
    first = one.publish(idempotency_key="one", priority=AnnouncementPriority.ASSERTIVE, message="Один")
    second = two.publish(idempotency_key="two", priority=AnnouncementPriority.ASSERTIVE, message="Два")
    assert (first.sequence, second.sequence) == (1, 2)
    assert [x.sequence for x in one.pending()] == [1, 2]
