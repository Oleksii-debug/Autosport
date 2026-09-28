import hashlib
import sqlite3

import pytest

from autosport.causal_collector import CollectorDelta, CollectorDeltaStore


_TRIGGER_NAME = "collector_deltas_projection_immutable_v1"
_MARKER_KEY = "indexed_projection_integrity_v1"
_ORDER_MARKER_KEY = "commit_order_integrity_v1"
_ORDER_UNVERIFIED_PREFIX = "commit_order_unverified_source_v1:"


def _install_noop_same_name_trigger(path, *, remove_marker: bool) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}")
        connection.execute(
            f"CREATE TRIGGER {_TRIGGER_NAME} AFTER UPDATE ON collector_deltas "
            "BEGIN SELECT 1; END"
        )
        if remove_marker:
            connection.execute(
                "DELETE FROM collector_meta "
                "WHERE key=? OR key=? OR key LIKE ?",
                (
                    _MARKER_KEY,
                    _ORDER_MARKER_KEY,
                    f"{_ORDER_UNVERIFIED_PREFIX}%",
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _trigger_sql(path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (_TRIGGER_NAME,),
        ).fetchone()
        assert row is not None
        assert isinstance(row[0], str)
        return row[0]
    finally:
        connection.close()


def test_durable_marker_rejects_same_name_noncanonical_trigger(tmp_path) -> None:
    path = tmp_path / "collector.db"
    CollectorDeltaStore(path)
    _install_noop_same_name_trigger(path, remove_marker=False)

    with pytest.raises(
        ValueError,
        match="projection integrity guard is missing or noncanonical",
    ):
        CollectorDeltaStore(path)


def test_pre_marker_same_name_trigger_is_reconciled_and_replaced(tmp_path) -> None:
    path = tmp_path / "collector.db"
    CollectorDeltaStore(path)
    _install_noop_same_name_trigger(path, remove_marker=True)

    CollectorDeltaStore(path)

    sql = " ".join(_trigger_sql(path).split())
    assert "BEFORE UPDATE OF commit_seq, delta_id, source_id, stream_epoch" in sql
    assert "SELECT RAISE(ABORT, 'collector delta indexed projections are immutable')" in sql
    assert "AFTER UPDATE ON collector_deltas BEGIN SELECT 1" not in sql

    connection = sqlite3.connect(path)
    try:
        marker = connection.execute(
            "SELECT value FROM collector_meta WHERE key=?",
            (_MARKER_KEY,),
        ).fetchone()
    finally:
        connection.close()
    assert marker == ("1",)

def test_commit_sequence_is_immutable_after_integrity_guard(tmp_path) -> None:
    path = tmp_path / "collector.db"
    CollectorDeltaStore(path)

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO collector_deltas("
            "delta_id, source_id, stream_epoch, cursor_position, revision_number, "
            "desktop_available_at, collector_committed_at, payload_sha256, payload_json"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "raw-trigger-probe",
                "source",
                "epoch",
                1,
                0,
                "2026-09-23T00:00:00Z",
                "2026-09-23T00:00:00Z",
                "0" * 64,
                "{}",
            ),
        )
        connection.commit()

        with pytest.raises(
            sqlite3.IntegrityError,
            match="collector delta indexed projections are immutable",
        ):
            connection.execute(
                "UPDATE collector_deltas SET commit_seq=commit_seq+100 "
                "WHERE delta_id='raw-trigger-probe'"
            )
    finally:
        connection.rollback()
        connection.close()

def _install_predecessor_projection_trigger(path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}")
        connection.execute(
            "DELETE FROM collector_meta WHERE key=? OR key LIKE ?",
            (_ORDER_MARKER_KEY, f"{_ORDER_UNVERIFIED_PREFIX}%"),
        )
        connection.execute(
            f"CREATE TRIGGER {_TRIGGER_NAME} BEFORE UPDATE OF "
            "delta_id, source_id, stream_epoch, cursor_position, revision_number, "
            "desktop_available_at, collector_committed_at "
            "ON collector_deltas BEGIN "
            "SELECT RAISE(ABORT, 'collector delta indexed projections are immutable'); END"
        )
        connection.commit()
    finally:
        connection.close()


def _install_successor_trigger_without_order_marker(path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}")
        connection.execute(
            "DELETE FROM collector_meta WHERE key=? OR key LIKE ?",
            (_ORDER_MARKER_KEY, f"{_ORDER_UNVERIFIED_PREFIX}%"),
        )
        connection.execute(
            f"CREATE TRIGGER {_TRIGGER_NAME} BEFORE UPDATE OF "
            "commit_seq, delta_id, source_id, stream_epoch, cursor_position, "
            "revision_number, desktop_available_at, collector_committed_at "
            "ON collector_deltas BEGIN "
            "SELECT RAISE(ABORT, "
            "'collector delta indexed projections are immutable'); END"
        )
        connection.commit()
    finally:
        connection.close()


def test_predecessor_canonical_trigger_upgrades_without_bricking_store(tmp_path) -> None:
    path = tmp_path / "collector.db"
    CollectorDeltaStore(path)
    _install_predecessor_projection_trigger(path)

    CollectorDeltaStore(path)

    sql = " ".join(_trigger_sql(path).split())
    assert "BEFORE UPDATE OF commit_seq, delta_id, source_id" in sql



def _delta(
    delta_id: str,
    cursor_position: int,
    *,
    stream_epoch: str = "epoch-1",
    revision_of: str | None = None,
    revision_number: int = 0,
    event_identity: str | None = None,
) -> CollectorDelta:
    identity = delta_id if event_identity is None else event_identity
    digest_seed = f"{delta_id}:{cursor_position}:{revision_number}".encode("utf-8")
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="source-x",
        lawful_terms_ref="terms:source-x:v1",
        retention_ref="retention:source-x:v1",
        stream_epoch=stream_epoch,
        source_cursor=str(cursor_position),
        cursor_position=cursor_position,
        event_dedupe_key=f"dedupe-{identity}",
        event_id=f"event-{identity}",
        source_payload_digest=hashlib.sha256(b"source:" + digest_seed).hexdigest(),
        canonical_event_digest=hashlib.sha256(
            b"canonical:" + digest_seed
        ).hexdigest(),
        source_observed_at="2026-09-23T00:00:00+00:00",
        collector_received_at="2026-09-23T00:00:01+00:00",
        collector_committed_at="2026-09-23T00:00:02+00:00",
        desktop_available_at="2026-09-23T00:00:03+00:00",
        revision_of=revision_of,
        revision_number=revision_number,
    )


def test_nonempty_predecessor_with_unique_order_upgrades_without_bricking_store(
    tmp_path,
) -> None:
    path = tmp_path / "collector.db"
    store = CollectorDeltaStore(path)
    assert store.append(_delta("d1", 1)) is True
    assert store.append(_delta("d2", 2)) is True
    _install_predecessor_projection_trigger(path)

    reopened = CollectorDeltaStore(path)

    assert tuple(
        delta.delta_id
        for delta in reopened.deltas_after_commit(source_id="source-x")
    ) == ("d1", "d2")


def test_predecessor_commit_order_tamper_fails_before_upgrade(tmp_path) -> None:
    path = tmp_path / "collector.db"
    store = CollectorDeltaStore(path)
    assert store.append(_delta("d1", 1)) is True
    assert store.append(_delta("d2", 2)) is True
    _install_predecessor_projection_trigger(path)

    connection = sqlite3.connect(path)
    try:
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
    finally:
        connection.close()

    with pytest.raises(
        ValueError,
        match="commit order conflicts with immutable causal history",
    ):
        CollectorDeltaStore(path)


def test_successor_without_order_marker_reconstructs_healthy_history(
    tmp_path,
) -> None:
    path = tmp_path / "collector.db"
    store = CollectorDeltaStore(path)
    assert store.append(_delta("d1", 1)) is True
    assert store.append(_delta("d2", 2)) is True
    _install_successor_trigger_without_order_marker(path)

    reopened = CollectorDeltaStore(path)

    assert tuple(
        delta.delta_id
        for delta in reopened.deltas_after_commit(source_id="source-x")
    ) == ("d1", "d2")


def test_successor_without_order_marker_detects_frozen_predecessor_reorder(
    tmp_path,
) -> None:
    path = tmp_path / "collector.db"
    store = CollectorDeltaStore(path)
    assert store.append(_delta("d1", 1)) is True
    assert store.append(_delta("d2", 2)) is True
    _install_predecessor_projection_trigger(path)

    connection = sqlite3.connect(path)
    try:
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
    finally:
        connection.close()

    _install_successor_trigger_without_order_marker(path)

    with pytest.raises(
        ValueError,
        match="commit order conflicts with immutable causal history",
    ):
        CollectorDeltaStore(path)


def test_ambiguous_predecessor_order_stays_readable_but_cursor_fails_closed(
    tmp_path,
) -> None:
    path = tmp_path / "collector.db"
    store = CollectorDeltaStore(path)
    assert store.append(_delta("d1", 1)) is True
    assert store.append(_delta("d2", 2)) is True
    assert store.append(
        _delta(
            "d1-r1",
            1,
            revision_of="d1",
            revision_number=1,
            event_identity="d1",
        )
    ) is True
    _install_predecessor_projection_trigger(path)

    reopened = CollectorDeltaStore(path)

    assert reopened.get("d1-r1") is not None
    with pytest.raises(
        ValueError,
        match="commit order is not independently verified",
    ):
        reopened.deltas_after_commit(source_id="source-x")


def test_unverified_predecessor_cannot_export_or_reuse_epoch_activation(
    tmp_path,
) -> None:
    path = tmp_path / "collector.db"
    store = CollectorDeltaStore(path)
    assert store.append(_delta("d1", 1, stream_epoch="epoch-1")) is True
    assert store.append(_delta("d2", 1, stream_epoch="epoch-2")) is True
    _install_predecessor_projection_trigger(path)

    # Model an activation issued by predecessor code while commit_seq was not
    # independently protected.  The new migration must preserve this row as
    # evidence but must not let an UNVERIFIED source export or reuse it.
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO collector_epoch_activations_v1("
            "source_id, generation, stream_epoch, activated_at"
            ") VALUES(?,?,?,?)",
            ("source-x", 1, "epoch-2", "2026-09-23T00:00:04+00:00"),
        )
        connection.commit()
    finally:
        connection.close()

    reopened = CollectorDeltaStore(path)

    with pytest.raises(
        ValueError,
        match="commit order is not independently verified",
    ):
        reopened.runtime_stream_epoch("source-x")

    with pytest.raises(
        ValueError,
        match="commit order is not independently verified",
    ):
        reopened._bootstrap_or_recover_runtime_stream_epoch(
            source_id="source-x",
            stream_epoch="epoch-2",
            activated_at="2026-09-23T00:00:05+00:00",
        )

    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT generation, stream_epoch, activated_at "
            "FROM collector_epoch_activations_v1 "
            "WHERE source_id='source-x' ORDER BY generation DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    assert row == (1, "epoch-2", "2026-09-23T00:00:04+00:00")
