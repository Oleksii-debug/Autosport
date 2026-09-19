import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from autosport.causal_collector import (
    AckConflictError,
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
)


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
    }


class DurableApplicationReceiptStore:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            self.path.write_text("{}\n", encoding="utf-8")

    def put(self, receipt):
        receipt.validate()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload[receipt.delta_id] = {
            "delta_id": receipt.delta_id,
            "canonical_event_digest": receipt.canonical_event_digest,
            "receipt_id": receipt.receipt_id,
            "applied_at": receipt.applied_at,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def get(self, delta):
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        item = payload.get(delta.delta_id)
        return None if item is None else DesktopApplicationReceipt(**item)


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
        revision_of=None,
        revision_number=0,
        gap_state=GapState.NONE,
        sync_state=None,
    ):
        payload = payload or event_payload()
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
            event_dedupe_key="source-x|event-1|winner|player-a|1",
            event_id=payload["event_id"],
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

    def test_consumer_applies_canonical_event_and_health_then_acks(self):
        payload = event_payload()
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.json")
            checkpoint = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            delta = self.make_delta()
            collector.append(delta)
            applied = []
            health = []
            consumer = DesktopDeltaConsumer(
                collector,
                checkpoint,
                resolve_event=lambda _: payload,
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
                apply_health=lambda d, e: health.append((d.delta_id, e["event_id"])),
            )
            self.assertEqual(
                consumer.drain(as_of="2026-01-01T00:00:05+00:00"),
                ("d1",),
            )
            self.assertEqual(applied, [payload])
            self.assertEqual(health, [("d1", "e1")])
            self.assertEqual(consumer.drain(as_of="2026-01-01T00:00:05+00:00"), ())
            self.assertTrue(DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json").has_ack("d1"))

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

            canonical_receipts = DurableApplicationReceiptStore(receipt_path)
            first_effects = []

            def apply_then_crash(current, event):
                first_effects.append(event)
                canonical_receipts.put(
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
                lookup_application_receipt=canonical_receipts.get,
            )
            with self.assertRaises(RuntimeError):
                first.drain(as_of="2026-01-01T00:00:05+00:00")
            self.assertEqual(len(first_effects), 1)
            self.assertFalse(DesktopDeltaCheckpointStore(desktop_path).has_ack("d1"))

            reopened_receipts = DurableApplicationReceiptStore(receipt_path)
            second_effects = []
            second = DesktopDeltaConsumer(
                CollectorDeltaStore(collector_path),
                DesktopDeltaCheckpointStore(desktop_path),
                resolve_event=lambda _: event_payload(),
                apply_event=lambda current, event: (
                    second_effects.append(event)
                    or (_ for _ in ()).throw(AssertionError("effect was replayed"))
                ),
                lookup_application_receipt=reopened_receipts.get,
            )
            self.assertEqual(second.drain(as_of="2026-01-01T00:00:05+00:00"), ("d1",))
            self.assertEqual(second_effects, [])
            self.assertTrue(DesktopDeltaCheckpointStore(desktop_path).has_ack("d1"))

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
