from __future__ import annotations

import hashlib
import sqlite3

import pytest

from autosport.causal_collector import CollectorDelta, CollectorDeltaStore


_TRIGGER_NAME = "collector_deltas_projection_immutable_v1"


def _delta(delta_id: str, cursor_position: int) -> CollectorDelta:
    digest_seed = f"{delta_id}:{cursor_position}".encode("utf-8")
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="source-x",
        lawful_terms_ref="terms:source-x:v1",
        retention_ref="retention:source-x:v1",
        stream_epoch="epoch-1",
        source_cursor=str(cursor_position),
        cursor_position=cursor_position,
        event_dedupe_key=f"dedupe-{delta_id}",
        event_id=f"event-{delta_id}",
        source_payload_digest=hashlib.sha256(b"source:" + digest_seed).hexdigest(),
        canonical_event_digest=hashlib.sha256(b"canonical:" + digest_seed).hexdigest(),
        source_observed_at="2026-09-23T00:00:00+00:00",
        collector_received_at="2026-09-23T00:00:01+00:00",
        collector_committed_at="2026-09-23T00:00:02+00:00",
        desktop_available_at="2026-09-23T00:00:03+00:00",
    )


def _install_predecessor_trigger_and_reverse_commit_order(path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"DROP TRIGGER {_TRIGGER_NAME}")
        connection.execute(
            f"CREATE TRIGGER {_TRIGGER_NAME} BEFORE UPDATE OF "
            "delta_id, source_id, stream_epoch, cursor_position, revision_number, "
            "desktop_available_at, collector_committed_at "
            "ON collector_deltas BEGIN "
            "SELECT RAISE(ABORT, "
            "'collector delta indexed projections are immutable'); END"
        )

        # The exact predecessor trigger did not protect commit_seq. Reorder two
        # already-authenticated rows without touching payload/projection bytes.
        connection.execute(
            "UPDATE collector_deltas SET commit_seq=100 WHERE delta_id='d1'"
        )
        connection.execute(
            "UPDATE collector_deltas SET commit_seq=1 WHERE delta_id='d2'"
        )
        connection.execute(
            "UPDATE collector_deltas SET commit_seq=2 WHERE delta_id='d1'"
        )
        connection.commit()

        rows = connection.execute(
            "SELECT delta_id, commit_seq FROM collector_deltas ORDER BY commit_seq"
        ).fetchall()
        assert rows == [("d2", 1), ("d1", 2)]
    finally:
        connection.close()


def test_predecessor_commit_order_tamper_cannot_be_laundered_by_upgrade(
    tmp_path,
) -> None:
    path = tmp_path / "collector.db"
    store = CollectorDeltaStore(path)
    assert store.append(_delta("d1", 1)) is True
    assert store.append(_delta("d2", 2)) is True

    _install_predecessor_trigger_and_reverse_commit_order(path)

    # Expected RED on #1783 parent: its predecessor-trigger migration recognizes
    # the old marker/trigger and installs the stronger trigger without proving
    # that commit_seq history remained authentic while it was still mutable.
    with pytest.raises(ValueError):
        CollectorDeltaStore(path)
