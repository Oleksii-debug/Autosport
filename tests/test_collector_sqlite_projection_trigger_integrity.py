import sqlite3

import pytest

from autosport.causal_collector import CollectorDeltaStore


_TRIGGER_NAME = "collector_deltas_projection_immutable_v1"
_MARKER_KEY = "indexed_projection_integrity_v1"


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
                "DELETE FROM collector_meta WHERE key=?",
                (_MARKER_KEY,),
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
            f"CREATE TRIGGER {_TRIGGER_NAME} BEFORE UPDATE OF "
            "delta_id, source_id, stream_epoch, cursor_position, revision_number, "
            "desktop_available_at, collector_committed_at "
            "ON collector_deltas BEGIN "
            "SELECT RAISE(ABORT, 'collector delta indexed projections are immutable'); END"
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

