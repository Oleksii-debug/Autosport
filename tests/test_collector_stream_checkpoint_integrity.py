import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    GapState,
    SyncState,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.domain import MarketEvent


def _delta(
    *,
    delta_id: str,
    cursor_position: int,
    stream_epoch: str = "epoch-1",
    sync_state: SyncState = SyncState.READY,
) -> CollectorDelta:
    payload = {
        "event_id": "e1",
        "market_id": "winner",
        "selection_id": "player-a",
        "decimal_odds": "1.80",
        "observed_ts": "2026-01-01T00:00:01+00:00",
        "source_id": "source-x",
        "sequence": 1,
        "market_type": "winner",
        "status": "open",
        "source_ts": "2026-01-01T00:00:00+00:00",
        "ingest_ts": "2026-01-01T00:00:01+00:00",
        "metadata": {},
        "score_state": None,
    }
    source_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="source-x",
        lawful_terms_ref="terms:source-x:v1",
        retention_ref="retention:source-x:v1",
        stream_epoch=stream_epoch,
        source_cursor=str(cursor_position),
        cursor_position=cursor_position,
        event_dedupe_key=MarketEvent.from_dict(payload).dedupe_key,
        event_id=payload["event_id"],
        source_payload_digest=digest_source_payload(source_payload),
        canonical_event_digest=canonical_event_digest(payload),
        source_observed_at="2026-01-01T00:00:01+00:00",
        collector_received_at="2026-01-01T00:00:02+00:00",
        collector_committed_at="2026-01-01T00:00:03+00:00",
        desktop_available_at="2026-01-01T00:00:04+00:00",
        revision_of=None,
        revision_number=0,
        gap_state=(
            GapState.CURSOR_RESET
            if sync_state == SyncState.CURSOR_RESET
            else GapState.NONE
        ),
        sync_state=sync_state,
    )


def test_deleted_checkpoint_with_immutable_history_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.json"
        first = _delta(delta_id="d1", cursor_position=1)
        store = CollectorDeltaStore(path)
        assert store.append(first)

        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "DELETE FROM collector_streams WHERE source_id=? AND stream_epoch=?",
                ("source-x", "epoch-1"),
            )
            connection.commit()
        finally:
            connection.close()

        reopened = CollectorDeltaStore(path)
        with pytest.raises(
            ValueError,
            match="stream checkpoint conflicts with immutable delta history",
        ):
            reopened.stream_checkpoint("source-x", "epoch-1")

        successor = _delta(delta_id="d2", cursor_position=2)
        with pytest.raises(
            ValueError,
            match="stream checkpoint conflicts with immutable delta history",
        ):
            reopened.append(successor)

        assert reopened.get(successor.delta_id) is None
        assert reopened.get(first.delta_id) == first


@pytest.mark.parametrize(
    "transition",
    [SyncState.EPOCH_CHANGED, SyncState.CURSOR_RESET],
)
def test_deleted_source_checkpoint_cannot_be_laundered_as_new_epoch(
    transition: SyncState,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.json"
        first = _delta(delta_id="d1", cursor_position=1)
        store = CollectorDeltaStore(path)
        assert store.append(first)

        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "DELETE FROM collector_streams WHERE source_id=?",
                ("source-x",),
            )
            connection.commit()
        finally:
            connection.close()

        reopened = CollectorDeltaStore(path)
        successor = _delta(
            delta_id=f"d2-{transition.value}",
            cursor_position=0,
            stream_epoch="epoch-2",
            sync_state=transition,
        )
        with pytest.raises(
            ValueError,
            match="stream checkpoint conflicts with immutable delta history",
        ):
            reopened.append(successor)

        assert reopened.get(successor.delta_id) is None
        assert reopened.get(first.delta_id) == first


def test_corrupted_surviving_checkpoint_cannot_authorize_new_epoch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.json"
        store = CollectorDeltaStore(path)
        first = _delta(delta_id="d1", cursor_position=1)
        second = _delta(delta_id="d2", cursor_position=2)
        assert store.append(first)
        assert store.append(second)

        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE collector_streams "
                "SET last_cursor=?, last_position=?, last_delta_id=? "
                "WHERE source_id=? AND stream_epoch=?",
                ("1", 1, "d1", "source-x", "epoch-1"),
            )
            connection.commit()
        finally:
            connection.close()

        reopened = CollectorDeltaStore(path)
        successor = _delta(
            delta_id="d3",
            cursor_position=0,
            stream_epoch="epoch-2",
            sync_state=SyncState.EPOCH_CHANGED,
        )
        with pytest.raises(
            ValueError,
            match="stream checkpoint conflicts with immutable delta history",
        ):
            reopened.append(successor)

        assert reopened.get(successor.delta_id) is None
        assert reopened.get(first.delta_id) == first
        assert reopened.get(second.delta_id) == second


def test_foreign_surviving_checkpoint_cannot_hide_deleted_epoch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.json"
        store = CollectorDeltaStore(path)
        first = _delta(delta_id="d1", cursor_position=1)
        assert store.append(first)

        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "DELETE FROM collector_streams WHERE source_id=?",
                ("source-x",),
            )
            connection.execute(
                "INSERT INTO collector_streams("
                "source_id, stream_epoch, last_cursor, last_position, last_delta_id"
                ") VALUES(?,?,?,?,?)",
                ("source-x", "epoch-foreign", "1", 1, "d1"),
            )
            connection.commit()
        finally:
            connection.close()

        reopened = CollectorDeltaStore(path)
        successor = _delta(
            delta_id="d2",
            cursor_position=0,
            stream_epoch="epoch-2",
            sync_state=SyncState.CURSOR_RESET,
        )
        with pytest.raises(
            ValueError,
            match="stream checkpoint conflicts with immutable delta history",
        ):
            reopened.append(successor)

        assert reopened.get(successor.delta_id) is None
        assert reopened.get(first.delta_id) == first


def test_legitimate_new_epoch_is_allowed_when_prior_checkpoint_survives() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.json"
        store = CollectorDeltaStore(path)
        first = _delta(delta_id="d1", cursor_position=1)
        successor = _delta(
            delta_id="d2",
            cursor_position=0,
            stream_epoch="epoch-2",
            sync_state=SyncState.EPOCH_CHANGED,
        )

        assert store.append(first)
        assert store.append(successor)
        assert store.get(successor.delta_id) == successor

def test_surviving_newer_checkpoint_cannot_hide_deleted_retained_epoch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.json"
        store = CollectorDeltaStore(path)
        epoch_1 = _delta(delta_id="d-e1", cursor_position=1)
        epoch_2 = _delta(
            delta_id="d-e2",
            cursor_position=0,
            stream_epoch="epoch-2",
            sync_state=SyncState.EPOCH_CHANGED,
        )
        assert store.append(epoch_1)
        assert store.append(epoch_2)

        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "DELETE FROM collector_streams "
                "WHERE source_id=? AND stream_epoch=?",
                ("source-x", "epoch-1"),
            )
            connection.commit()
        finally:
            connection.close()

        reopened = CollectorDeltaStore(path)
        epoch_3 = _delta(
            delta_id="d-e3",
            cursor_position=0,
            stream_epoch="epoch-3",
            sync_state=SyncState.EPOCH_CHANGED,
        )
        with pytest.raises(
            ValueError,
            match="stream checkpoint conflicts with immutable delta history",
        ):
            reopened.append(epoch_3)

        assert reopened.get(epoch_3.delta_id) is None
        assert reopened.get(epoch_1.delta_id) == epoch_1
        assert reopened.get(epoch_2.delta_id) == epoch_2


def test_intact_multi_epoch_checkpoint_chain_admits_third_epoch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CollectorDeltaStore(Path(tmp) / "collector.json")
        epoch_1 = _delta(delta_id="d-e1", cursor_position=1)
        epoch_2 = _delta(
            delta_id="d-e2",
            cursor_position=0,
            stream_epoch="epoch-2",
            sync_state=SyncState.EPOCH_CHANGED,
        )
        epoch_3 = _delta(
            delta_id="d-e3",
            cursor_position=0,
            stream_epoch="epoch-3",
            sync_state=SyncState.EPOCH_CHANGED,
        )

        assert store.append(epoch_1)
        assert store.append(epoch_2)
        assert store.append(epoch_3)
        assert store.get(epoch_1.delta_id) == epoch_1
        assert store.get(epoch_2.delta_id) == epoch_2
        assert store.get(epoch_3.delta_id) == epoch_3
