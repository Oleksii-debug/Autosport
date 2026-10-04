import json
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from autosport.causal_collector import (
    AckConflictError,
    CanonicalDesktopApplication,
    ApplicationReceiptError,
    CausalView,
    CollectorDelta,
    CollectorDeltaStore,
    CursorRegressionError,
    DeltaConflictError,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    DesktopDeltaConsumer,
    GapState,
    GapStateError,
    SyncState,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.domain import MarketEvent
from autosport.ingestion_health import SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.storage import SQLiteMarketStore


def event_payload(*, event_id="e1", odds="1.80"):
    return {
        "event_id": event_id,
        "market_id": "winner",
        "selection_id": "player-a",
        "decimal_odds": odds,
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


class DurableApplicationReceiptStore:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            self.path.write_text("{}\n", encoding="utf-8")

    def put(self, receipt):
        receipt.validate()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data[receipt.delta_id] = {
            "delta_id": receipt.delta_id,
            "canonical_event_digest": receipt.canonical_event_digest,
            "receipt_id": receipt.receipt_id,
            "applied_at": receipt.applied_at,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def get(self, delta):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        item = data.get(delta.delta_id)
        return None if item is None else DesktopApplicationReceipt(**item)


class CollectorDeltaTests(unittest.TestCase):
    def make_delta(
        self,
        *,
        delta_id="d1",
        cursor_position=1,
        epoch="epoch-1",
        available="2026-01-01T00:00:04+00:00",
        payload=None,
        source_payload=None,
        revision_of=None,
        revision_number=0,
        gap_state=GapState.NONE,
        sync_state=None,
    ):
        payload = payload or event_payload()
        if source_payload is None:
            source_payload = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )
        if sync_state is None:
            sync_state = {
                GapState.NONE: SyncState.READY,
                GapState.DETECTED: SyncState.GAP_DETECTED,
                GapState.RECOVERED: SyncState.RECOVERED,
                GapState.CURSOR_RESET: SyncState.CURSOR_RESET,
            }[gap_state]
        return CollectorDelta(
            schema_version=1,
            delta_id=delta_id,
            source_id="source-x",
            lawful_terms_ref="terms:source-x:v1",
            retention_ref="retention:source-x:v1",
            stream_epoch=epoch,
            source_cursor=str(cursor_position),
            cursor_position=cursor_position,
            event_dedupe_key=MarketEvent.from_dict(payload).dedupe_key,
            event_id=payload["event_id"],
            source_payload_digest=digest_source_payload(source_payload),
            canonical_event_digest=canonical_event_digest(payload),
            source_observed_at="2026-01-01T00:00:01+00:00",
            collector_received_at="2026-01-01T00:00:02+00:00",
            collector_committed_at="2026-01-01T00:00:03+00:00",
            desktop_available_at=available,
            revision_of=revision_of,
            revision_number=revision_number,
            gap_state=gap_state,
            sync_state=sync_state,
            gap_from_cursor="1" if gap_state in {GapState.DETECTED, GapState.RECOVERED} else None,
            gap_to_cursor="2" if gap_state in {GapState.DETECTED, GapState.RECOVERED} else None,
        )

    def test_timestamps_are_causal(self):
        delta = self.make_delta()
        with self.assertRaises(ValueError):
            replace(delta, collector_received_at="2025-01-01T00:00:00+00:00").validate()

    def test_append_reopen_and_idempotence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            store = CollectorDeltaStore(path)
            delta = self.make_delta()
            self.assertTrue(store.append(delta))
            self.assertFalse(store.append(delta))
            reopened = CollectorDeltaStore(path)
            self.assertEqual(reopened.get("d1"), delta)
            checkpoint = reopened.stream_checkpoint("source-x", "epoch-1")
            self.assertEqual(checkpoint.last_position, 1)
            self.assertEqual(checkpoint.last_cursor, "1")

    def test_source_and_normalized_digests_preserve_distinct_evidence(self):
        payload = event_payload()
        source_payload = json.dumps(payload, ensure_ascii=False, indent=2)
        delta = self.make_delta(payload=payload, source_payload=source_payload)
        self.assertEqual(
            delta.source_payload_digest,
            digest_source_payload(source_payload),
        )
        self.assertNotEqual(delta.source_payload_digest, delta.canonical_event_digest)
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            store.append(delta)
            self.assertEqual(store.get(delta.delta_id), delta)

    def test_conflicting_duplicate_id_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            first = self.make_delta()
            store.append(first)
            conflict = replace(
                first,
                event_id="evil",
                canonical_event_digest=canonical_event_digest(event_payload(event_id="evil")),
            )
            with self.assertRaises(DeltaConflictError):
                store.append(conflict)

    def test_cursor_regression_requires_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            store.append(self.make_delta(delta_id="d2", cursor_position=2))
            with self.assertRaises(CursorRegressionError):
                store.append(self.make_delta(delta_id="d1", cursor_position=1))

    def test_revision_can_correct_historical_cursor_without_regressing_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            first = self.make_delta(delta_id="d1", cursor_position=1)
            latest = self.make_delta(delta_id="d2", cursor_position=2)
            correction = self.make_delta(
                delta_id="d1r",
                cursor_position=1,
                revision_of="d1",
                revision_number=1,
            )
            store.append(first)
            store.append(latest)
            store.append(correction)
            checkpoint = store.stream_checkpoint("source-x", "epoch-1")
            self.assertEqual(checkpoint.last_position, 2)
            self.assertEqual(checkpoint.last_delta_id, "d2")

    def test_new_epoch_requires_explicit_epoch_change_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            store.append(self.make_delta(delta_id="d9", cursor_position=9))
            with self.assertRaises(CursorRegressionError):
                store.append(self.make_delta(delta_id="bad-reset", cursor_position=0, epoch="epoch-2"))

    def test_new_epoch_allows_cursor_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            store.append(self.make_delta(delta_id="d9", cursor_position=9))
            reset = self.make_delta(
                delta_id="reset", cursor_position=0, epoch="epoch-2", gap_state=GapState.CURSOR_RESET
            )
            self.assertTrue(store.append(reset))
            checkpoint = store.stream_checkpoint("source-x", "epoch-2")
            self.assertEqual(checkpoint.last_position, 0)

    def test_future_backfill_is_hidden_until_desktop_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            early = self.make_delta(delta_id="early", cursor_position=1, available="2026-01-01T00:00:04+00:00")
            late = self.make_delta(delta_id="late", cursor_position=2, available="2026-01-01T00:00:10+00:00")
            store.append(early)
            store.append(late)
            known = store.deltas_available_through(
                as_of="2026-01-01T00:00:05+00:00",
                view=CausalView.AS_KNOWN_AT_DECISION,
            )
            research = store.deltas_available_through(
                as_of="2026-01-01T00:00:11+00:00",
                view=CausalView.RESTATED_RESEARCH,
            )
            self.assertEqual([item.delta_id for item in known], ["early"])
            self.assertEqual({item.delta_id for item in research}, {"early", "late"})

    def test_late_correction_preserves_decision_view_and_restates_research(self):
        original_payload = event_payload(odds="1.80")
        corrected_payload = event_payload(odds="1.95")
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            original = self.make_delta(
                delta_id="d1",
                cursor_position=1,
                payload=original_payload,
                available="2026-01-01T00:00:04+00:00",
            )
            correction = self.make_delta(
                delta_id="d1r",
                cursor_position=1,
                payload=corrected_payload,
                available="2026-01-01T00:00:10+00:00",
                revision_of="d1",
                revision_number=1,
            )
            collector.append(original)
            collector.append(correction)

            decision_applied = []
            decision_consumer = DesktopDeltaConsumer(
                collector,
                DesktopDeltaCheckpointStore(Path(tmp) / "decision-desktop.json"),
                resolve_event=lambda delta: (
                    original_payload if delta.delta_id == "d1" else corrected_payload
                ),
                apply_event=lambda delta, event: (
                    decision_applied.append(delta.delta_id)
                    or DesktopApplicationReceipt(
                        delta_id=delta.delta_id,
                        canonical_event_digest=canonical_event_digest(event),
                        receipt_id=f"receipt:{delta.delta_id}",
                        applied_at="2026-01-01T00:00:05+00:00",
                    )
                ),
                lookup_application_receipt=lambda delta: None,
            )
            self.assertEqual(
                decision_consumer.drain(
                    as_of="2026-01-01T00:00:05+00:00",
                    view=CausalView.AS_KNOWN_AT_DECISION,
                ),
                ("d1",),
            )
            self.assertEqual(decision_applied, ["d1"])

            research_applied = []
            research_consumer = DesktopDeltaConsumer(
                collector,
                DesktopDeltaCheckpointStore(Path(tmp) / "research-desktop.json"),
                resolve_event=lambda delta: (
                    original_payload if delta.delta_id == "d1" else corrected_payload
                ),
                apply_event=lambda delta, event: (
                    research_applied.append(delta.delta_id)
                    or DesktopApplicationReceipt(
                        delta_id=delta.delta_id,
                        canonical_event_digest=canonical_event_digest(event),
                        receipt_id=f"receipt:{delta.delta_id}",
                        applied_at="2026-01-01T00:00:11+00:00",
                    )
                ),
                lookup_application_receipt=lambda delta: None,
            )
            self.assertEqual(
                research_consumer.drain(
                    as_of="2026-01-01T00:00:11+00:00",
                    view=CausalView.RESTATED_RESEARCH,
                ),
                ("d1r",),
            )
            self.assertEqual(research_applied, ["d1r"])
            self.assertEqual(correction.revision_of, "d1")
            self.assertEqual(correction.revision_number, 1)

    def test_end_to_end_offline_reconnect_survives_restart(self):
        payload1 = event_payload(event_id="e1")
        payload2 = event_payload(event_id="e2", odds="1.90")
        with tempfile.TemporaryDirectory() as tmp:
            collector_path = Path(tmp) / "collector.json"
            desktop_path = Path(tmp) / "desktop.json"
            collector = CollectorDeltaStore(collector_path)
            collector.append(self.make_delta(delta_id="d1", cursor_position=1, payload=payload1))
            collector.append(
                self.make_delta(
                    delta_id="d2",
                    cursor_position=2,
                    payload=payload2,
                    available="2026-01-01T00:00:10+00:00",
                )
            )

            first_applied = []
            first_consumer = DesktopDeltaConsumer(
                collector,
                DesktopDeltaCheckpointStore(desktop_path),
                resolve_event=lambda delta: payload1 if delta.delta_id == "d1" else payload2,
                apply_event=lambda delta, event: (
                    first_applied.append(event)
                    or DesktopApplicationReceipt(
                        delta_id=delta.delta_id,
                        canonical_event_digest=canonical_event_digest(event),
                        receipt_id=f"receipt-{delta.delta_id}",
                        applied_at="2026-01-01T00:00:04+00:00",
                    )
                ),
                lookup_application_receipt=lambda delta: None,
            )
            self.assertEqual(
                first_consumer.drain(as_of="2026-01-01T00:00:05+00:00"),
                ("d1",),
            )
            self.assertEqual(first_applied, [payload1])

            reopened_collector = CollectorDeltaStore(collector_path)
            reopened_desktop = DesktopDeltaCheckpointStore(desktop_path)
            second_applied = []
            second_consumer = DesktopDeltaConsumer(
                reopened_collector,
                reopened_desktop,
                resolve_event=lambda delta: payload1 if delta.delta_id == "d1" else payload2,
                apply_event=lambda delta, event: (
                    second_applied.append(event)
                    or DesktopApplicationReceipt(
                        delta_id=delta.delta_id,
                        canonical_event_digest=canonical_event_digest(event),
                        receipt_id=f"receipt-{delta.delta_id}",
                        applied_at="2026-01-01T00:00:11+00:00",
                    )
                ),
                lookup_application_receipt=lambda delta: None,
            )
            self.assertEqual(
                second_consumer.drain(as_of="2026-01-01T00:00:11+00:00"),
                ("d2",),
            )
            self.assertEqual(second_applied, [payload2])

            final_desktop = DesktopDeltaCheckpointStore(desktop_path)
            self.assertEqual(
                final_desktop.stream_checkpoint("source-x", "epoch-1").last_position,
                2,
            )
            self.assertEqual(second_consumer.drain(as_of="2026-01-01T00:00:11+00:00"), ())

    def test_canonical_application_first_open_preserves_peer_completed_receipt(self):
        event = MarketEvent.from_dict(event_payload())
        delta = self.make_delta(payload=event.to_dict())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            peer_path = root / "peer-application.json"
            peer_market = SQLiteMarketStore(root / "peer-market.db")
            try:
                peer_application = CanonicalDesktopApplication(
                    MarketEventBus(peer_market),
                    SourceHealthStore(root / "peer-health.json"),
                    peer_path,
                    clock=lambda: "2026-01-01T00:00:05+00:00",
                )
                receipt = peer_application.apply(delta, event)
                receipt.validate()
            finally:
                peer_market.close()

            peer_state = json.loads(peer_path.read_text(encoding="utf-8"))
            target_path = root / "canonical-application.json"

            @contextmanager
            def peer_publishes_before_lock_owner_reads(_workspace):
                target_path.write_text(
                    json.dumps(
                        peer_state,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n",
                    encoding="utf-8",
                )
                yield

            with patch(
                "autosport.causal_collector_legacy.WorkspaceEconomicLock",
                side_effect=peer_publishes_before_lock_owner_reads,
            ):
                reopened = CanonicalDesktopApplication(
                    object(),
                    object(),
                    target_path,
                    clock=lambda: "2026-01-01T00:00:06+00:00",
                )

            self.assertEqual(
                reopened.completed_receipts_for_source("source-x"),
                (receipt,),
            )
            self.assertEqual(
                json.loads(target_path.read_text(encoding="utf-8")),
                peer_state,
            )

    def test_checkpoint_first_open_preserves_peer_state_created_at_lock_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "desktop.json"
            peer_state = {
                "schema_version": 1,
                "acks": [
                    {
                        "delta_id": "peer-delta",
                        "canonical_event_digest": "1" * 64,
                        "acknowledged_at": "2026-01-01T00:00:05+00:00",
                        "application_receipt_id": "receipt-peer",
                        "applied_at": "2026-01-01T00:00:04+00:00",
                    }
                ],
                "streams": {},
            }

            @contextmanager
            def peer_publishes_before_lock_owner_reads(_workspace):
                path.write_text(
                    json.dumps(peer_state, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                yield

            with patch(
                "autosport.causal_collector_legacy.WorkspaceEconomicLock",
                side_effect=peer_publishes_before_lock_owner_reads,
            ):
                checkpoint = DesktopDeltaCheckpointStore(path)

            self.assertTrue(checkpoint.has_ack("peer-delta"))
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                peer_state,
            )

    def test_checkpoint_ack_refreshes_state_after_serialization_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "desktop.json"
            first = DesktopDeltaCheckpointStore(path)
            second = DesktopDeltaCheckpointStore(path)
            first_delta = self.make_delta(delta_id="d1", cursor_position=1)
            second_delta = self.make_delta(
                delta_id="d2",
                cursor_position=2,
                payload=event_payload(event_id="e2"),
            )
            first_receipt = DesktopApplicationReceipt(
                delta_id=first_delta.delta_id,
                canonical_event_digest=first_delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:04+00:00",
            )
            second_receipt = DesktopApplicationReceipt(
                delta_id=second_delta.delta_id,
                canonical_event_digest=second_delta.canonical_event_digest,
                receipt_id="receipt-d2",
                applied_at="2026-01-01T00:00:04+00:00",
            )
            interleaved = []

            @contextmanager
            def interleaving_lock():
                if not interleaved:
                    interleaved.append(True)
                    self.assertTrue(
                        second.ack(
                            second_delta,
                            application_receipt=second_receipt,
                            acknowledged_at="2026-01-01T00:00:05+00:00",
                        )
                    )
                yield

            first._workspace_lock = interleaving_lock  # type: ignore[method-assign]
            self.assertTrue(
                first.ack(
                    first_delta,
                    application_receipt=first_receipt,
                    acknowledged_at="2026-01-01T00:00:05+00:00",
                )
            )

            reopened = DesktopDeltaCheckpointStore(path)
            self.assertTrue(reopened.has_ack("d1"))
            self.assertTrue(reopened.has_ack("d2"))
            checkpoint = reopened.stream_checkpoint("source-x", "epoch-1")
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.last_position, 2)
            self.assertEqual(checkpoint.last_delta_id, "d2")

    def test_consumer_uses_ack_clock_after_causal_visibility_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            deliveries = []
            receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:05+00:00",
            )
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=lambda *_: receipt,
                lookup_application_receipt=lambda _: None,
                acknowledgement_clock=lambda: "2026-01-01T00:00:06+00:00",
                on_application_receipt=lambda *_: deliveries.append(True),
            )

            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:04+00:00"),
                ("d1",),
            )
            self.assertEqual(deliveries, [True])
            self.assertTrue(checkpoint.has_ack(delta.delta_id))

    def test_consumer_persists_post_delivery_ack_clock_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:05+00:00",
            )
            clock_values = iter(
                (
                    "2026-01-01T00:00:06+00:00",
                    "2026-01-01T00:00:07+00:00",
                )
            )
            deliveries = []
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=lambda *_: receipt,
                lookup_application_receipt=lambda _: None,
                acknowledgement_clock=lambda: next(clock_values),
                on_application_receipt=lambda *_: deliveries.append("delivered"),
            )

            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:04+00:00"),
                ("d1",),
            )
            self.assertEqual(deliveries, ["delivered"])
            raw = checkpoint._read()
            self.assertEqual(len(raw["acks"]), 1)
            self.assertEqual(
                raw["acks"][0]["acknowledged_at"],
                "2026-01-01T00:00:07+00:00",
            )

    def test_consumer_drain_seals_ack_validator_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:05+00:00",
            )
            deliveries = []
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=lambda *_: receipt,
                lookup_application_receipt=lambda _: None,
                acknowledgement_clock=lambda: "2026-01-01T00:00:06+00:00",
                on_application_receipt=lambda *_: deliveries.append("delivered"),
            )

            original = DesktopDeltaConsumer._acknowledged_at

            def forged_ack_validator(*_args, **_kwargs):
                raise AssertionError("mutable class ACK validator must not be dispatched")

            DesktopDeltaConsumer._acknowledged_at = forged_ack_validator
            try:
                self.assertEqual(
                    consumer.drain(as_of="2026-01-01T00:00:04+00:00"),
                    ("d1",),
                )
            finally:
                DesktopDeltaConsumer._acknowledged_at = original

            self.assertEqual(deliveries, ["delivered"])
            self.assertTrue(checkpoint.has_ack(delta.delta_id))

    def test_consumer_post_delivery_ack_clock_rollback_leaves_ack_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:05+00:00",
            )
            clock_values = iter(
                (
                    "2026-01-01T00:00:06+00:00",
                    "2026-01-01T00:00:05+00:00",
                )
            )
            deliveries = []
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=lambda *_: self.fail(
                    "apply_event must not run when durable receipt exists"
                ),
                lookup_application_receipt=lambda _: receipt,
                acknowledgement_clock=lambda: next(clock_values),
                on_application_receipt=lambda *_: deliveries.append("delivered"),
            )

            with self.assertRaisesRegex(
                ApplicationReceiptError,
                "acknowledgement clock moved backward after receipt delivery",
            ):
                consumer.drain(as_of="2026-01-01T00:00:04+00:00")
            self.assertEqual(deliveries, ["delivered"])
            self.assertFalse(checkpoint.has_ack(delta.delta_id))

    def test_consumer_rejects_ack_clock_rollback_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:05+00:00",
            )
            deliveries = []
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=lambda *_: self.fail(
                    "apply_event must not run when durable receipt exists"
                ),
                lookup_application_receipt=lambda _: receipt,
                acknowledgement_clock=lambda: "2026-01-01T00:00:03+00:00",
                on_application_receipt=lambda *_: deliveries.append(True),
            )

            with self.assertRaisesRegex(
                ApplicationReceiptError,
                "acknowledgement clock moved before the causal drain cutoff",
            ):
                consumer.drain(as_of="2026-01-01T00:00:04+00:00")
            self.assertEqual(deliveries, [])
            self.assertFalse(checkpoint.has_ack(delta.delta_id))

    def test_consumer_legacy_cutoff_rejects_future_durable_receipt_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            future_receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:07+00:00",
            )
            deliveries = []
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=lambda *_: self.fail(
                    "apply_event must not run when durable receipt exists"
                ),
                lookup_application_receipt=lambda _: future_receipt,
                on_application_receipt=lambda *_: deliveries.append(True),
            )

            with self.assertRaisesRegex(
                ApplicationReceiptError,
                "desktop_available_at <= applied_at <= acknowledged_at",
            ):
                consumer.drain(as_of="2026-01-01T00:00:06+00:00")
            self.assertEqual(deliveries, [])
            self.assertFalse(checkpoint.has_ack(delta.delta_id))

    def test_consumer_rejects_preavailability_fresh_receipt_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            deliveries = []
            applications = []

            def apply_event(current, resolved):
                self.assertEqual(resolved, event)
                applications.append(current.delta_id)
                return DesktopApplicationReceipt(
                    delta_id=current.delta_id,
                    canonical_event_digest=current.canonical_event_digest,
                    receipt_id="receipt-d1",
                    applied_at="2026-01-01T00:00:03+00:00",
                )

            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=apply_event,
                lookup_application_receipt=lambda _: None,
                acknowledgement_clock=lambda: "2026-01-01T00:00:06+00:00",
                on_application_receipt=lambda *_: deliveries.append(True),
            )

            with self.assertRaisesRegex(
                ApplicationReceiptError,
                "desktop_available_at <= applied_at <= acknowledged_at",
            ):
                consumer.drain(as_of="2026-01-01T00:00:04+00:00")
            self.assertEqual(applications, [delta.delta_id])
            self.assertEqual(deliveries, [])
            self.assertFalse(checkpoint.has_ack(delta.delta_id))

    def test_consumer_rejects_misbound_durable_receipt_before_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            wrong_receipt = DesktopApplicationReceipt(
                delta_id="other-delta",
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-other",
                applied_at="2026-01-01T00:00:05+00:00",
            )
            deliveries = []
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=lambda *_: self.fail(
                    "apply_event must not run when durable receipt exists"
                ),
                lookup_application_receipt=lambda _: wrong_receipt,
                on_application_receipt=lambda *_: deliveries.append(True),
            )

            with self.assertRaisesRegex(
                ApplicationReceiptError,
                "durable application receipt is not bound to delta d1",
            ):
                consumer.drain(as_of="2026-01-01T00:00:06+00:00")
            self.assertEqual(deliveries, [])
            self.assertFalse(checkpoint.has_ack(delta.delta_id))

    def test_consumer_retries_post_receipt_delivery_before_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            receipt_store = DurableApplicationReceiptStore(root / "receipts.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            apply_calls = []
            delivery_calls = []

            def apply_event(current, resolved):
                self.assertEqual(resolved, event)
                apply_calls.append(current.delta_id)
                receipt = DesktopApplicationReceipt(
                    delta_id=current.delta_id,
                    canonical_event_digest=current.canonical_event_digest,
                    receipt_id="receipt-d1",
                    applied_at="2026-01-01T00:00:05+00:00",
                )
                receipt_store.put(receipt)
                return receipt

            def deliver(current, receipt):
                self.assertEqual(receipt.delta_id, current.delta_id)
                delivery_calls.append(current.delta_id)
                if len(delivery_calls) == 1:
                    raise RuntimeError("post-receipt-delivery-failed")

            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=apply_event,
                lookup_application_receipt=receipt_store.get,
                on_application_receipt=deliver,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "post-receipt-delivery-failed",
            ):
                consumer.drain(as_of="2026-01-01T00:00:06+00:00")
            self.assertFalse(checkpoint.has_ack(delta.delta_id))
            self.assertEqual(apply_calls, ["d1"])
            self.assertEqual(delivery_calls, ["d1"])

            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:06+00:00"),
                ("d1",),
            )
            self.assertTrue(checkpoint.has_ack(delta.delta_id))
            self.assertEqual(apply_calls, ["d1"])
            self.assertEqual(delivery_calls, ["d1", "d1"])
            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:06+00:00"),
                (),
            )
            self.assertEqual(delivery_calls, ["d1", "d1"])

    def test_consumer_digest_rejects_wrong_event_after_market_serializer_rebind(self):
        expected_payload = event_payload()
        wrong_event = MarketEvent.from_dict(
            {
                **expected_payload,
                "decimal_odds": "9.99",
            }
        )
        forged_payload = dict(expected_payload)

        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            collector.append(self.make_delta(payload=expected_payload))

            def must_not_apply(_delta, _event):
                raise AssertionError("digest mismatch must fail before application")

            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: wrong_event,
                apply_event=must_not_apply,
                lookup_application_receipt=lambda _delta: None,
            )

            def forged_to_dict(_event):
                return dict(forged_payload)

            with patch.object(MarketEvent, "to_dict", forged_to_dict):
                with self.assertRaises(DeltaConflictError):
                    consumer.drain(as_of="2026-01-01T00:00:05+00:00")

            self.assertFalse(checkpoint.has_ack("d1"))


    def test_consumer_rejects_market_event_subclass_before_application_or_ack(self):
        expected_payload = event_payload()

        class ForgedMarketEvent(MarketEvent):
            def to_dict(self):
                return dict(expected_payload)

        forged = ForgedMarketEvent.from_dict(
            {**expected_payload, "decimal_odds": "9.99"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            delta = self.make_delta(payload=expected_payload)
            collector.append(delta)
            apply_calls = []

            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: forged,
                apply_event=lambda *_: apply_calls.append(True),
                lookup_application_receipt=lambda _: None,
            )
            with self.assertRaisesRegex(
                DeltaConflictError,
                "exact MarketEvent",
            ):
                consumer.drain(as_of="2026-01-01T00:00:05+00:00")

            self.assertEqual(apply_calls, [])
            self.assertFalse(checkpoint.has_ack(delta.delta_id))

    def test_consumer_rejects_receipt_subclass_before_delivery_or_ack(self):
        class ForgedReceipt(DesktopApplicationReceipt):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            forged = ForgedReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="forged-receipt",
                applied_at="2026-01-01T00:00:05+00:00",
            )
            deliveries = []
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=lambda *_: self.fail("apply_event must not run"),
                lookup_application_receipt=lambda _: forged,
                on_application_receipt=lambda *_: deliveries.append(True),
            )
            with self.assertRaisesRegex(
                ApplicationReceiptError,
                "canonical receipt type",
            ):
                consumer.drain(as_of="2026-01-01T00:00:06+00:00")
            self.assertEqual(deliveries, [])
            self.assertFalse(checkpoint.has_ack(delta.delta_id))

    def test_consumer_rejects_fresh_receipt_subclass_before_delivery_or_ack(self):
        class ForgedReceipt(DesktopApplicationReceipt):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            payload = event_payload()
            event = MarketEvent.from_dict(payload)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            forged = ForgedReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="forged-receipt",
                applied_at="2026-01-01T00:00:05+00:00",
            )
            deliveries = []
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event,
                apply_event=lambda *_: forged,
                lookup_application_receipt=lambda _: None,
                on_application_receipt=lambda *_: deliveries.append(True),
            )
            with self.assertRaisesRegex(
                ApplicationReceiptError,
                "apply_event must return a durable DesktopApplicationReceipt",
            ):
                consumer.drain(as_of="2026-01-01T00:00:06+00:00")
            self.assertEqual(deliveries, [])
            self.assertFalse(checkpoint.has_ack(delta.delta_id))

    def test_consumer_rechecks_peer_ack_after_serialization_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            desktop_path = root / "desktop.json"
            primary_checkpoint = DesktopDeltaCheckpointStore(desktop_path)
            peer_checkpoint = DesktopDeltaCheckpointStore(desktop_path)
            delta = self.make_delta()
            collector.append(delta)
            receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:04+00:00",
            )
            applied = []
            interleaved = []

            @contextmanager
            def peer_ack_before_locked_read():
                if not interleaved:
                    interleaved.append(True)
                    self.assertTrue(
                        peer_checkpoint.ack(
                            delta,
                            application_receipt=receipt,
                            acknowledged_at="2026-01-01T00:00:05+00:00",
                        )
                    )
                yield

            primary_checkpoint._workspace_lock = peer_ack_before_locked_read  # type: ignore[method-assign]
            consumer = DesktopDeltaConsumer(
                collector,
                primary_checkpoint,
                resolve_event=lambda _: event_payload(),
                apply_event=lambda current, event: (
                    applied.append(current.delta_id)
                    or receipt
                ),
                lookup_application_receipt=lambda current: None,
            )

            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:05+00:00"),
                (),
            )
            self.assertEqual(applied, [])
            self.assertTrue(DesktopDeltaCheckpointStore(desktop_path).has_ack("d1"))

    def test_consumer_refreshes_durable_receipt_after_serialization_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = CollectorDeltaStore(root / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(root / "desktop.json")
            receipt_store = DurableApplicationReceiptStore(root / "application-receipts.json")
            delta = self.make_delta()
            collector.append(delta)
            receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:04+00:00",
            )
            published = []
            reapplied = []

            @contextmanager
            def receipt_publish_before_locked_read():
                if not published:
                    published.append(True)
                    receipt_store.put(receipt)
                yield

            checkpoint._workspace_lock = receipt_publish_before_locked_read  # type: ignore[method-assign]
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event_payload(),
                apply_event=lambda current, event: (
                    reapplied.append(current.delta_id)
                    or receipt
                ),
                lookup_application_receipt=receipt_store.get,
            )

            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:05+00:00"),
                ("d1",),
            )
            self.assertEqual(reapplied, [])
            self.assertTrue(DesktopDeltaCheckpointStore(root / "desktop.json").has_ack("d1"))

    def test_consumer_rejects_separate_health_application_boundary(self):
        payload = event_payload()
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            collector.append(self.make_delta())
            with self.assertRaises(ApplicationReceiptError):
                DesktopDeltaConsumer(
                    collector,
                    checkpoint,
                    resolve_event=lambda _: payload,
                    apply_event=lambda delta, event: DesktopApplicationReceipt(
                        delta_id=delta.delta_id,
                        canonical_event_digest=canonical_event_digest(event),
                        receipt_id=f"receipt-{delta.delta_id}",
                        applied_at="2026-01-01T00:00:04+00:00",
                    ),
                    lookup_application_receipt=lambda delta: None,
                    apply_health=lambda d, e: None,
                )

    def test_consumer_never_acks_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            collector.append(self.make_delta())
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event_payload(odds="2.10"),
                apply_event=lambda delta, event: DesktopApplicationReceipt(
                    delta_id=delta.delta_id,
                    canonical_event_digest=canonical_event_digest(event),
                    receipt_id=f"receipt-{delta.delta_id}",
                    applied_at="2026-01-01T00:00:04+00:00",
                ),
                lookup_application_receipt=lambda delta: None,
            )
            with self.assertRaises(DeltaConflictError):
                consumer.drain(as_of="2026-01-01T00:00:05+00:00")
            self.assertFalse(checkpoint.has_ack("d1"))

    def test_revision_rejects_cross_source_even_with_same_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            store.append(self.make_delta(delta_id="d1", cursor_position=1))
            cross_source = replace(
                self.make_delta(
                    delta_id="d1r",
                    cursor_position=1,
                    revision_of="d1",
                    revision_number=1,
                ),
                source_id="source-evil",
            )
            with self.assertRaises(CursorRegressionError):
                store.append(cross_source)

    def test_revision_rejects_cross_epoch_even_with_same_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            store.append(self.make_delta(delta_id="d1", cursor_position=1))
            cross_epoch = self.make_delta(
                delta_id="d1r",
                cursor_position=1,
                epoch="epoch-2",
                revision_of="d1",
                revision_number=1,
                gap_state=GapState.CURSOR_RESET,
            )
            with self.assertRaises(CursorRegressionError):
                store.append(cross_epoch)

    def test_gap_recovery_rejects_cross_event_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            detected = self.make_delta(delta_id="gap", cursor_position=1, gap_state=GapState.DETECTED)
            store.append(detected)
            wrong_event = self.make_delta(
                delta_id="gap-recovered",
                cursor_position=1,
                revision_of="gap",
                revision_number=1,
                gap_state=GapState.RECOVERED,
                payload=event_payload(event_id="different"),
            )
            with self.assertRaises(CursorRegressionError):
                store.append(wrong_event)

    def test_unresolved_gap_blocks_application(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            collector.append(self.make_delta(gap_state=GapState.DETECTED))
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event_payload(),
                apply_event=lambda delta, event: DesktopApplicationReceipt(
                    delta_id=delta.delta_id,
                    canonical_event_digest=canonical_event_digest(event),
                    receipt_id=f"receipt-{delta.delta_id}",
                    applied_at="2026-01-01T00:00:04+00:00",
                ),
                lookup_application_receipt=lambda delta: None,
            )
            with self.assertRaises(GapStateError):
                consumer.drain(as_of="2026-01-01T00:00:05+00:00")
            self.assertFalse(checkpoint.has_ack("d1"))

    def test_recovered_gap_revision_can_pass_without_acknowledging_detected_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            detected = self.make_delta(delta_id="gap", cursor_position=1, gap_state=GapState.DETECTED)
            recovered = self.make_delta(
                delta_id="gap-recovered",
                cursor_position=1,
                revision_of="gap",
                revision_number=1,
                gap_state=GapState.RECOVERED,
            )
            collector.append(detected)
            collector.append(recovered)
            applied = []
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event_payload(),
                apply_event=lambda delta, event: (
                    applied.append(event)
                    or DesktopApplicationReceipt(
                        delta_id=delta.delta_id,
                        canonical_event_digest=canonical_event_digest(event),
                        receipt_id=f"receipt-{delta.delta_id}",
                        applied_at="2026-01-01T00:00:04+00:00",
                    )
                ),
                lookup_application_receipt=lambda delta: None,
            )
            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:05+00:00"),
                ("gap-recovered",),
            )
            self.assertFalse(checkpoint.has_ack("gap"))

    def test_ack_requires_exact_event_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            delta = self.make_delta()
            collector.append(delta)
            with self.assertRaises(ApplicationReceiptError):
                checkpoint.ack(
                    delta,
                    application_receipt=DesktopApplicationReceipt(
                        delta_id=delta.delta_id,
                        canonical_event_digest="0" * 64,
                        receipt_id="receipt-1",
                        applied_at="2026-01-01T00:00:04+00:00",
                    ),
                    acknowledged_at="2026-01-01T00:00:05+00:00",
                )

    def test_callback_failure_happens_before_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            collector.append(self.make_delta())
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: event_payload(),
                apply_event=lambda delta, event: (_ for _ in ()).throw(RuntimeError("apply failed")),
                lookup_application_receipt=lambda delta: None,
            )
            with self.assertRaises(RuntimeError):
                consumer.drain(as_of="2026-01-01T00:00:05+00:00")
            self.assertFalse(checkpoint.has_ack("d1"))

    def test_consumer_preserves_receipt_lookup_callable(self):
        payload = event_payload()
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            desktop = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            delta = self.make_delta()
            collector.append(delta)
            lookup_calls = []

            def lookup(current):
                lookup_calls.append(current.delta_id)
                return None

            consumer = DesktopDeltaConsumer(
                collector,
                desktop,
                resolve_event=lambda _: payload,
                apply_event=lambda current, event: DesktopApplicationReceipt(
                    delta_id=current.delta_id,
                    canonical_event_digest=canonical_event_digest(event),
                    receipt_id=f"receipt-{current.delta_id}",
                    applied_at="2026-01-01T00:00:04+00:00",
                ),
                lookup_application_receipt=lookup,
            )
            self.assertIs(consumer.lookup_application_receipt, lookup)
            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:05+00:00"),
                ("d1",),
            )
            self.assertEqual(lookup_calls, ["d1"])

    def test_ack_rejects_application_before_desktop_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            delta = self.make_delta()
            receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:03+00:00",
            )
            with self.assertRaises(ApplicationReceiptError):
                checkpoint.ack(
                    delta,
                    application_receipt=receipt,
                    acknowledged_at="2026-01-01T00:00:05+00:00",
                )

    def test_ack_rejects_application_after_acknowledgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            delta = self.make_delta()
            receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:06+00:00",
            )
            with self.assertRaises(ApplicationReceiptError):
                checkpoint.ack(
                    delta,
                    application_receipt=receipt,
                    acknowledged_at="2026-01-01T00:00:05+00:00",
                )

    def test_restart_uses_durable_application_receipt_without_reapplying_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            desktop = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            delta = self.make_delta()
            collector.append(delta)
            receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id="receipt-d1",
                applied_at="2026-01-01T00:00:04+00:00",
            )
            # A separate canonical application authority has already persisted
            # the effect+receipt, while the desktop acknowledgement was lost.
            reapplied = []
            consumer = DesktopDeltaConsumer(
                collector,
                desktop,
                resolve_event=lambda _: event_payload(),
                apply_event=lambda delta, event: (
                    reapplied.append(event)
                    or receipt
                ),
                lookup_application_receipt=lambda current: receipt,
            )
            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:05+00:00"),
                ("d1",),
            )
            self.assertEqual(reapplied, [])

    def test_apply_event_must_return_bound_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            desktop = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            delta = self.make_delta()
            collector.append(delta)
            consumer = DesktopDeltaConsumer(
                collector,
                desktop,
                resolve_event=lambda _: event_payload(),
                apply_event=lambda current, event: DesktopApplicationReceipt(
                    delta_id="wrong",
                    canonical_event_digest=canonical_event_digest(event),
                    receipt_id="wrong",
                    applied_at="2026-01-01T00:00:04+00:00",
                ),
                lookup_application_receipt=lambda current: None,
            )
            with self.assertRaises(ApplicationReceiptError):
                consumer.drain(as_of="2026-01-01T00:00:05+00:00")

    def test_crash_after_application_before_ack_recovers_durable_receipt_without_reapply(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector_path = Path(tmp) / "collector.json"
            desktop_path = Path(tmp) / "desktop.json"
            receipt_path = Path(tmp) / "application-receipts.json"
            collector = CollectorDeltaStore(collector_path)
            delta = self.make_delta()
            collector.append(delta)
            receipt_store = DurableApplicationReceiptStore(receipt_path)
            applied_once = []

            def apply_then_crash(current, event):
                applied_once.append(event)
                receipt_store.put(
                    DesktopApplicationReceipt(
                        delta_id=current.delta_id,
                        canonical_event_digest=canonical_event_digest(event),
                        receipt_id=f"receipt:{current.delta_id}",
                        applied_at="2026-01-01T00:00:04+00:00",
                    )
                )
                raise RuntimeError("crash-after-application-before-ack")

            first = DesktopDeltaConsumer(
                collector,
                DesktopDeltaCheckpointStore(desktop_path),
                resolve_event=lambda _: event_payload(),
                apply_event=apply_then_crash,
                lookup_application_receipt=receipt_store.get,
            )
            with self.assertRaises(RuntimeError):
                first.drain(as_of="2026-01-01T00:00:05+00:00")
            self.assertEqual(len(applied_once), 1)
            self.assertFalse(DesktopDeltaCheckpointStore(desktop_path).has_ack("d1"))

            reopened_receipts = DurableApplicationReceiptStore(receipt_path)
            replayed = []
            second = DesktopDeltaConsumer(
                CollectorDeltaStore(collector_path),
                DesktopDeltaCheckpointStore(desktop_path),
                resolve_event=lambda _: event_payload(),
                apply_event=lambda current, event: (
                    replayed.append(event)
                    or (_ for _ in ()).throw(AssertionError("durable application was replayed"))
                ),
                lookup_application_receipt=reopened_receipts.get,
            )
            self.assertEqual(
                second.drain(as_of="2026-01-01T00:00:05+00:00"),
                ("d1",),
            )
            self.assertEqual(replayed, [])
            self.assertTrue(DesktopDeltaCheckpointStore(desktop_path).has_ack("d1"))


    def test_canonical_application_rejects_market_event_subclass_before_durable_effects(self):
        expected_payload = event_payload()

        class ForgedMarketEvent(MarketEvent):
            def to_dict(self):
                return dict(expected_payload)

        forged = ForgedMarketEvent.from_dict(
            {**expected_payload, "decimal_odds": "9.99"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            delta = self.make_delta(payload=expected_payload)
            market_store = SQLiteMarketStore(root / "market.db")
            health_store = SourceHealthStore(root / "health.json")
            application = CanonicalDesktopApplication(
                MarketEventBus(market_store),
                health_store,
                root / "canonical-application.json",
                clock=lambda: "2026-01-01T00:00:04+00:00",
            )

            with self.assertRaisesRegex(
                DeltaConflictError,
                "exact MarketEvent",
            ):
                application.apply(delta, forged)

            self.assertEqual(market_store.events("e1"), [])
            self.assertEqual(health_store.get("source-x").poll_count, 0)
            self.assertIsNone(application.lookup_receipt(delta))
            market_store.close()

    def test_canonical_application_receipt_time_is_after_durable_completion(self):
        event = MarketEvent.from_dict(event_payload())
        payload = event.to_dict()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector_path = root / "collector.json"
            desktop_path = root / "desktop.json"
            market_path = root / "market.db"
            health_path = root / "health.json"
            application_path = root / "canonical-application.json"

            collector = CollectorDeltaStore(collector_path)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            market_store = SQLiteMarketStore(market_path)
            real_bus = MarketEventBus(market_store)

            class CrashBeforeMarketBus:
                def __init__(self):
                    self.crashed = False

                def publish(self, current):
                    if not self.crashed:
                        self.crashed = True
                        raise RuntimeError("crash-after-prepare-before-market")
                    return real_bus.publish(current)

            first_application = CanonicalDesktopApplication(
                CrashBeforeMarketBus(),
                SourceHealthStore(health_path),
                application_path,
                clock=lambda: "2026-01-01T00:00:04+00:00",
            )
            first_consumer = DesktopDeltaConsumer(
                collector,
                DesktopDeltaCheckpointStore(desktop_path),
                resolve_event=lambda _: event,
                apply_event=first_application.apply,
                lookup_application_receipt=first_application.lookup_receipt,
            )
            with self.assertRaises(RuntimeError):
                first_consumer.drain(as_of="2026-01-01T00:00:05+00:00")
            self.assertIsNone(first_application.lookup_receipt(delta))
            self.assertEqual(market_store.events("e1"), [])
            self.assertEqual(SourceHealthStore(health_path).get("source-x").poll_count, 0)

            reopened_application = CanonicalDesktopApplication(
                real_bus,
                SourceHealthStore(health_path),
                application_path,
                clock=lambda: "2026-01-01T00:00:06+00:00",
            )
            reopened_consumer = DesktopDeltaConsumer(
                CollectorDeltaStore(collector_path),
                DesktopDeltaCheckpointStore(desktop_path),
                resolve_event=lambda _: event,
                apply_event=reopened_application.apply,
                lookup_application_receipt=reopened_application.lookup_receipt,
            )
            self.assertEqual(
                reopened_consumer.drain(as_of="2026-01-01T00:00:07+00:00"),
                ("d1",),
            )
            receipt = reopened_application.lookup_receipt(delta)
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt.applied_at, "2026-01-01T00:00:06+00:00")
            self.assertEqual(len(market_store.events("e1")), 1)
            self.assertEqual(SourceHealthStore(health_path).get("source-x").poll_count, 1)
            market_store.close()

    def test_canonical_application_persists_market_health_and_receipt_across_restart(self):
        event = MarketEvent.from_dict(event_payload())
        payload = event.to_dict()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector_path = root / "collector.json"
            desktop_path = root / "desktop.json"
            market_path = root / "market.db"
            health_path = root / "health.json"
            application_path = root / "canonical-application.json"

            collector = CollectorDeltaStore(collector_path)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            market_store = SQLiteMarketStore(market_path)
            health_store = SourceHealthStore(health_path)
            application = CanonicalDesktopApplication(
                MarketEventBus(market_store),
                health_store,
                application_path,
                clock=lambda: "2026-01-01T00:00:04+00:00",
            )
            consumer = DesktopDeltaConsumer(
                collector,
                DesktopDeltaCheckpointStore(desktop_path),
                resolve_event=lambda _: event,
                apply_event=application.apply,
                lookup_application_receipt=application.lookup_receipt,
            )
            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:05+00:00"),
                ("d1",),
            )
            market_store.close()

            reopened_market = SQLiteMarketStore(market_path)
            self.assertEqual(reopened_market.events("e1"), [event])
            reopened_health = SourceHealthStore(health_path).get("source-x")
            self.assertEqual(reopened_health.poll_count, 1)
            self.assertEqual(reopened_health.total_accepted, 1)
            self.assertEqual(reopened_health.last_cursor, "1")

            reopened_application = CanonicalDesktopApplication(
                MarketEventBus(reopened_market),
                SourceHealthStore(health_path),
                application_path,
                clock=lambda: "2026-01-01T00:00:05+00:00",
            )
            reopened_consumer = DesktopDeltaConsumer(
                CollectorDeltaStore(collector_path),
                DesktopDeltaCheckpointStore(desktop_path),
                resolve_event=lambda _: event,
                apply_event=reopened_application.apply,
                lookup_application_receipt=reopened_application.lookup_receipt,
            )
            self.assertEqual(
                reopened_consumer.drain(as_of="2026-01-01T00:00:05+00:00"),
                (),
            )
            self.assertEqual(len(reopened_market.events("e1")), 1)
            self.assertEqual(SourceHealthStore(health_path).get("source-x").poll_count, 1)
            reopened_market.close()

    def test_canonical_application_recovers_crash_after_health_without_double_apply(self):
        event = MarketEvent.from_dict(event_payload())
        payload = event.to_dict()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector_path = root / "collector.json"
            desktop_path = root / "desktop.json"
            market_path = root / "market.db"
            health_path = root / "health.json"
            application_path = root / "canonical-application.json"

            collector = CollectorDeltaStore(collector_path)
            delta = self.make_delta(payload=payload)
            collector.append(delta)
            market_store = SQLiteMarketStore(market_path)
            application = CanonicalDesktopApplication(
                MarketEventBus(market_store),
                SourceHealthStore(health_path),
                application_path,
                clock=lambda: "2026-01-01T00:00:04+00:00",
            )
            original_mark_health = application._state.mark_health_applied

            def crash_before_health_progress_marker(current):
                raise RuntimeError("crash-after-canonical-health-before-progress-marker")

            application._state.mark_health_applied = crash_before_health_progress_marker
            first = DesktopDeltaConsumer(
                collector,
                DesktopDeltaCheckpointStore(desktop_path),
                resolve_event=lambda _: event,
                apply_event=application.apply,
                lookup_application_receipt=application.lookup_receipt,
            )
            with self.assertRaises(RuntimeError):
                first.drain(as_of="2026-01-01T00:00:05+00:00")
            application._state.mark_health_applied = original_mark_health
            self.assertFalse(DesktopDeltaCheckpointStore(desktop_path).has_ack("d1"))
            self.assertEqual(SourceHealthStore(health_path).get("source-x").poll_count, 1)
            self.assertEqual(len(market_store.events("e1")), 1)
            market_store.close()

            reopened_market = SQLiteMarketStore(market_path)
            reopened_application = CanonicalDesktopApplication(
                MarketEventBus(reopened_market),
                SourceHealthStore(health_path),
                application_path,
                clock=lambda: "2026-01-01T00:00:05+00:00",
            )
            second = DesktopDeltaConsumer(
                CollectorDeltaStore(collector_path),
                DesktopDeltaCheckpointStore(desktop_path),
                resolve_event=lambda _: event,
                apply_event=reopened_application.apply,
                lookup_application_receipt=reopened_application.lookup_receipt,
            )
            self.assertEqual(
                second.drain(as_of="2026-01-01T00:00:05+00:00"),
                ("d1",),
            )
            self.assertTrue(DesktopDeltaCheckpointStore(desktop_path).has_ack("d1"))
            self.assertEqual(SourceHealthStore(health_path).get("source-x").poll_count, 1)
            self.assertEqual(len(reopened_market.events("e1")), 1)
            reopened_market.close()

    def test_crash_before_atomic_replace_preserves_previous_durable_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            store = CollectorDeltaStore(path)
            store.append(self.make_delta())
            original_write = store._write
            store._write = lambda _: (_ for _ in ()).throw(RuntimeError("simulated crash"))  # type: ignore[method-assign]
            with self.assertRaises(RuntimeError):
                store.append(self.make_delta(delta_id="d2", cursor_position=2))
            store._write = original_write  # type: ignore[method-assign]
            reopened = CollectorDeltaStore(path)
            self.assertEqual([item.delta_id for item in reopened._all()], ["d1"])


if __name__ == "__main__":
    unittest.main()
