from __future__ import annotations

import pytest

from autosport.causal_collector import (
    ApplicationReceiptError,
    CanonicalDesktopApplication,
    CollectorDelta,
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    DesktopDeltaConsumer,
    GapState,
    SyncState,
    canonical_event_digest,
)
from autosport.domain import MarketEvent
from autosport.ingestion_health import SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.storage import SQLiteMarketStore


H = "a" * 64
T0 = "2026-09-22T00:00:00+00:00"
T1 = "2026-09-22T00:00:01+00:00"
T2 = "2026-09-22T00:00:02+00:00"
T3 = "2026-09-22T00:00:03+00:00"


def _delta(*, delta_id: str, cursor_position: int) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="source-a",
        lawful_terms_ref="terms-v1",
        retention_ref="retention-v1",
        stream_epoch="epoch-1",
        source_cursor=f"cursor-{cursor_position}",
        cursor_position=cursor_position,
        event_dedupe_key=f"event-{cursor_position}",
        event_id=f"event-{cursor_position}",
        source_payload_digest=H,
        canonical_event_digest=H,
        source_observed_at=T0,
        collector_received_at=T1,
        collector_committed_at=T2,
        desktop_available_at=T3,
        gap_state=GapState.NONE,
        sync_state=SyncState.READY,
    )



def _event(*, event_id: str = "event-1", odds: str = "1.80") -> MarketEvent:
    return MarketEvent.from_dict(
        {
            "event_id": event_id,
            "market_id": "winner",
            "selection_id": "player-a",
            "decimal_odds": odds,
            "observed_ts": T1,
            "source_id": "source-a",
            "sequence": 1,
            "market_type": "winner",
            "status": "open",
            "source_ts": T0,
            "ingest_ts": T1,
            "metadata": {},
            "score_state": None,
        }
    )


def _canonical_delta(
    event: MarketEvent,
    *,
    delta_id: str = "delta-1",
    cursor_position: int = 1,
) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id=event.source_id,
        lawful_terms_ref="terms-v1",
        retention_ref="retention-v1",
        stream_epoch="epoch-1",
        source_cursor=f"cursor-{cursor_position}",
        cursor_position=cursor_position,
        event_dedupe_key=event.dedupe_key,
        event_id=event.event_id,
        source_payload_digest=H,
        canonical_event_digest=canonical_event_digest(event),
        source_observed_at=T0,
        collector_received_at=T1,
        collector_committed_at=T2,
        desktop_available_at=T3,
        gap_state=GapState.NONE,
        sync_state=SyncState.READY,
    )


def _caller_receipt(delta: CollectorDelta) -> DesktopApplicationReceipt:
    return DesktopApplicationReceipt(
        delta_id=delta.delta_id,
        canonical_event_digest=delta.canonical_event_digest,
        receipt_id=f"caller-forged:{delta.delta_id}",
        applied_at=T3,
    )


def _assert_rejected_without_ack(
    checkpoint: DesktopDeltaCheckpointStore,
    delta: CollectorDelta,
) -> None:
    try:
        accepted = checkpoint.ack(
            delta,
            application_receipt=_caller_receipt(delta),
            acknowledged_at=T3,
        )
    except ApplicationReceiptError:
        accepted = False

    assert accepted is False
    assert not checkpoint.has_ack(delta.delta_id)


def test_caller_constructed_receipt_cannot_mint_first_desktop_ack(tmp_path) -> None:
    """A structural receipt is not durable application authority."""

    checkpoint = DesktopDeltaCheckpointStore(tmp_path / "desktop-acks.json")
    delta = _delta(delta_id="delta-1", cursor_position=1)

    _assert_rejected_without_ack(checkpoint, delta)
    assert checkpoint.stream_checkpoint(delta.source_id, delta.stream_epoch) is None


def test_caller_receipt_cannot_skip_unacked_durable_predecessor(tmp_path) -> None:
    """Highest-contiguous ACK cannot be advanced by bypassing the consumer."""

    collector = CollectorDeltaStore(tmp_path / "collector-deltas.json")
    checkpoint = DesktopDeltaCheckpointStore(tmp_path / "desktop-acks.json")
    first = _delta(delta_id="delta-1", cursor_position=1)
    second = _delta(delta_id="delta-2", cursor_position=2)
    assert collector.append(first)
    assert collector.append(second)
    assert not checkpoint.has_ack(first.delta_id)

    _assert_rejected_without_ack(checkpoint, second)

    current = checkpoint.stream_checkpoint(second.source_id, second.stream_epoch)
    assert current is None or current.last_position < second.cursor_position


def test_forged_consumer_callbacks_cannot_mint_desktop_ack(tmp_path) -> None:
    event = _event()
    delta = _canonical_delta(event)
    collector = CollectorDeltaStore(tmp_path / "collector.db")
    checkpoint = DesktopDeltaCheckpointStore(tmp_path / "desktop-acks.json")
    assert collector.append(delta)
    forged_receipt = DesktopApplicationReceipt(
        delta_id=delta.delta_id,
        canonical_event_digest=delta.canonical_event_digest,
        receipt_id="caller-forged:consumer",
        applied_at=T3,
    )
    consumer = DesktopDeltaConsumer(
        collector,
        checkpoint,
        resolve_event=lambda _current: event,
        apply_event=lambda _current, _event: forged_receipt,
        lookup_application_receipt=lambda _current: None,
    )

    with pytest.raises(
        ApplicationReceiptError,
        match="CanonicalDesktopApplication durable receipt authority",
    ):
        consumer.drain(as_of=T3)

    assert not checkpoint.has_ack(delta.delta_id)


def test_canonical_durable_application_can_publish_desktop_ack(tmp_path) -> None:
    event = _event()
    delta = _canonical_delta(event)
    collector = CollectorDeltaStore(tmp_path / "collector.db")
    checkpoint = DesktopDeltaCheckpointStore(tmp_path / "desktop-acks.json")
    assert collector.append(delta)

    market_store = SQLiteMarketStore(tmp_path / "market.db")
    application = CanonicalDesktopApplication(
        MarketEventBus(market_store),
        SourceHealthStore(tmp_path / "health.json"),
        tmp_path / "canonical-application.json",
        clock=lambda: T3,
    )
    consumer = DesktopDeltaConsumer(
        collector,
        checkpoint,
        resolve_event=lambda _current: event,
        apply_event=application.apply,
        lookup_application_receipt=application.lookup_receipt,
    )

    assert consumer.drain(as_of=T3) == (delta.delta_id,)
    durable = application.lookup_receipt(delta)
    assert durable is not None
    assert checkpoint.application_receipt(delta) == durable
    market_store.close()
