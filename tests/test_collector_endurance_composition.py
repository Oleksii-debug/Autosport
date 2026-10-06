from __future__ import annotations

import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    CollectorStorageBackpressureError,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    GapState,
    SyncState,
)
from autosport.collector_retention import CollectorRetentionManager


SOURCE_ID = "endurance-source"
OLD_EPOCH = "epoch-1"
NEW_EPOCH = "epoch-2"
SOURCE_OBSERVED_AT = "2026-01-01T00:00:00+00:00"
COLLECTOR_RECEIVED_AT = "2026-01-01T00:00:01+00:00"
COLLECTOR_COMMITTED_AT = "2026-01-01T00:00:02+00:00"
DESKTOP_AVAILABLE_AT = "2026-01-01T00:00:03+00:00"
APPLIED_AT = "2026-01-01T00:00:04+00:00"
ACKNOWLEDGED_AT = "2026-01-01T00:00:05+00:00"
COMPACTED_AT = "2026-01-02T00:00:00+00:00"


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _delta(
    position: int,
    *,
    epoch: str = OLD_EPOCH,
    delta_id: str | None = None,
    padding: int = 0,
    gap_state: GapState = GapState.NONE,
    sync_state: SyncState = SyncState.READY,
    revision_of: str | None = None,
    revision_number: int = 0,
    gap_from_cursor: str | None = None,
    gap_to_cursor: str | None = None,
    event_position: int | None = None,
) -> CollectorDelta:
    event_position = position if event_position is None else event_position
    event_id = f"{SOURCE_ID}:event-{event_position}"
    cursor = f"cursor-{position}"
    identity = delta_id or f"{epoch}-delta-{position}-r{revision_number}"
    return CollectorDelta(
        schema_version=1,
        delta_id=identity,
        source_id=SOURCE_ID,
        lawful_terms_ref="terms:endurance:v1:" + ("x" * padding),
        retention_ref="retention:endurance:v1",
        stream_epoch=epoch,
        source_cursor=cursor,
        cursor_position=position,
        event_dedupe_key=f"{SOURCE_ID}|event-{event_position}",
        event_id=event_id,
        source_payload_digest=_digest(f"source:{identity}:{padding}"),
        canonical_event_digest=_digest(f"canonical:{event_position}:{revision_number}"),
        source_observed_at=SOURCE_OBSERVED_AT,
        collector_received_at=COLLECTOR_RECEIVED_AT,
        collector_committed_at=COLLECTOR_COMMITTED_AT,
        desktop_available_at=DESKTOP_AVAILABLE_AT,
        revision_of=revision_of,
        revision_number=revision_number,
        gap_state=gap_state,
        sync_state=sync_state,
        gap_from_cursor=gap_from_cursor,
        gap_to_cursor=gap_to_cursor,
    )


def _recovered(predecessor: CollectorDelta) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id=f"{predecessor.delta_id}-recovered",
        source_id=predecessor.source_id,
        lawful_terms_ref=predecessor.lawful_terms_ref,
        retention_ref=predecessor.retention_ref,
        stream_epoch=predecessor.stream_epoch,
        source_cursor=predecessor.source_cursor,
        cursor_position=predecessor.cursor_position,
        event_dedupe_key=predecessor.event_dedupe_key,
        event_id=predecessor.event_id,
        source_payload_digest=_digest(f"source:{predecessor.delta_id}:recovered"),
        canonical_event_digest=_digest(f"canonical:{predecessor.delta_id}:recovered"),
        source_observed_at=predecessor.source_observed_at,
        collector_received_at=predecessor.collector_received_at,
        collector_committed_at=predecessor.collector_committed_at,
        desktop_available_at=predecessor.desktop_available_at,
        revision_of=predecessor.delta_id,
        revision_number=predecessor.revision_number + 1,
        gap_state=GapState.RECOVERED,
        sync_state=SyncState.RECOVERED,
        gap_from_cursor=predecessor.gap_from_cursor,
        gap_to_cursor=predecessor.gap_to_cursor,
    )


def _ack(checkpoint: DesktopDeltaCheckpointStore, delta: CollectorDelta) -> None:
    receipt = DesktopApplicationReceipt(
        delta_id=delta.delta_id,
        canonical_event_digest=delta.canonical_event_digest,
        receipt_id=f"receipt:{delta.delta_id}",
        applied_at=APPLIED_AT,
    )
    checkpoint.ack(
        delta,
        application_receipt=receipt,
        acknowledged_at=ACKNOWLEDGED_AT,
    )


def _page_geometry(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path)
    try:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        return page_size, page_count
    finally:
        connection.close()


def test_collector_composed_endurance_restart_retention_budget_and_multi_handle(
    tmp_path: Path,
) -> None:
    """Exercise the collector durability authorities as one no-sleep scenario."""

    collector_path = tmp_path / "collector.sqlite"
    desktop_path = tmp_path / "desktop.json"

    collector = CollectorDeltaStore(collector_path)
    desktop = DesktopDeltaCheckpointStore(desktop_path)

    old_history: list[CollectorDelta] = []
    for position in range(1, 97):
        delta = _delta(position, padding=4096)
        assert collector._append_with_runtime_stream_epoch(
            delta,
            activated_at=COLLECTOR_COMMITTED_AT,
        )
        old_history.append(delta)

    # Duplicate truth is idempotent under a second canonical handle.
    peer_before_restart = CollectorDeltaStore(collector_path)
    assert peer_before_restart.append(old_history[0]) is False

    detected = _delta(
        97,
        padding=4096,
        gap_state=GapState.DETECTED,
        sync_state=SyncState.GAP_DETECTED,
        gap_from_cursor="cursor-96",
        gap_to_cursor="cursor-97",
    )
    assert collector._append_with_runtime_stream_epoch(
        detected,
        activated_at=COLLECTOR_COMMITTED_AT,
    )
    old_history.append(detected)

    recovered = _recovered(detected)
    assert collector._append_with_runtime_stream_epoch(
        recovered,
        activated_at=COLLECTOR_COMMITTED_AT,
    )
    old_history.append(recovered)

    terminal = _delta(98, padding=4096)
    assert collector._append_with_runtime_stream_epoch(
        terminal,
        activated_at=COLLECTOR_COMMITTED_AT,
    )
    old_history.append(terminal)

    # Move product-owned active-epoch authority before old-epoch compaction.
    new_epoch_anchor = _delta(
        0,
        epoch=NEW_EPOCH,
        delta_id="epoch-2-anchor",
        sync_state=SyncState.EPOCH_CHANGED,
    )
    assert collector._append_with_runtime_stream_epoch(
        new_epoch_anchor,
        activated_at="2026-01-01T00:00:06+00:00",
    )
    assert collector.runtime_stream_epoch(SOURCE_ID) == (NEW_EPOCH, 2)

    # Desktop application acknowledgement makes old evidence eligible for the
    # retention planner while preserving the terminal transport anchor.
    for delta in old_history:
        _ack(desktop, delta)

    # Construct retention metadata before fixing the byte ceiling so its schema is
    # included in the durable baseline budget.
    CollectorRetentionManager(collector)
    page_size, page_count = _page_geometry(collector_path)
    max_bytes = (page_count + 16) * page_size

    bounded = CollectorDeltaStore(collector_path, max_bytes=max_bytes)
    peer = CollectorDeltaStore(collector_path)
    assert bounded.configured_max_bytes == max_bytes
    assert peer.configured_max_bytes == max_bytes
    assert peer.runtime_stream_epoch(SOURCE_ID) == (NEW_EPOCH, 2)

    # A large current-epoch append cannot silently exceed the durable budget.
    pressure_delta = _delta(
        1,
        epoch=NEW_EPOCH,
        delta_id="epoch-2-pressure",
        padding=page_size * 48,
    )
    with pytest.raises(
        CollectorStorageBackpressureError,
        match="RETENTION_REQUIRED",
    ):
        peer.append(pressure_delta)
    assert CollectorDeltaStore(collector_path).get(pressure_delta.delta_id) is None

    # A restart sees exactly the same old history and durable budget before
    # compaction. No in-memory handle is authoritative.
    reopened = CollectorDeltaStore(collector_path)
    assert reopened.configured_max_bytes == max_bytes
    assert reopened.get(terminal.delta_id) == terminal
    assert reopened.get(new_epoch_anchor.delta_id) == new_epoch_anchor

    manager = CollectorRetentionManager(reopened)
    plan = manager.preview(
        source_id=SOURCE_ID,
        stream_epoch=OLD_EPOCH,
        desktop_checkpoint=DesktopDeltaCheckpointStore(desktop_path),
    )
    assert len(plan.delete_delta_ids) >= 80
    assert terminal.delta_id in plan.retained_delta_ids
    assert plan.terminal_checkpoint_delta_id == terminal.delta_id
    assert plan.desktop_transport_anchor_delta_id == terminal.delta_id

    result = manager.compact(
        plan,
        desktop_checkpoint=DesktopDeltaCheckpointStore(desktop_path),
        compacted_at=COMPACTED_AT,
    )
    assert result.deleted_delta_ids == plan.delete_delta_ids
    assert result.reclamation_complete

    # Physical reclamation plus the unchanged durable page ceiling creates room for
    # the exact append that correctly failed closed under pressure before compaction.
    after_compaction = CollectorDeltaStore(collector_path)
    assert after_compaction.configured_max_bytes == max_bytes
    assert after_compaction.runtime_stream_epoch(SOURCE_ID) == (NEW_EPOCH, 2)
    assert after_compaction.append(pressure_delta) is True

    # A second canonical handle observes the committed row and exact retry is
    # idempotent, proving the composed scenario is path-authoritative across handles.
    final_peer = CollectorDeltaStore(collector_path)
    assert final_peer.get(pressure_delta.delta_id) == pressure_delta
    assert final_peer.append(pressure_delta) is False

    journal = CollectorRetentionManager(final_peer).compaction_journal()
    assert len(journal) == 1
    assert journal[0]["plan_id"] == plan.plan_id
    assert journal[0]["reclamation_complete"] is True
