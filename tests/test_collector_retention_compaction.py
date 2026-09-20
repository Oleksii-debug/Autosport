import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    DeltaConflictError,
    GapState,
    SyncState,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.collector_retention import (
    CollectorRetentionError,
    CollectorRetentionManager,
    CollectorRetentionPlanStaleError,
    RetentionPinKind,
)
from autosport.domain import MarketEvent


def make_delta(
    *,
    delta_id: str,
    position: int,
    epoch: str = "epoch-1",
    sync_state: SyncState = SyncState.READY,
    revision_of: str | None = None,
    revision_number: int = 0,
    odds: str = "1.80",
) -> CollectorDelta:
    payload = {
        "event_id": "source-x:event-1",
        "market_id": "source-x:winner",
        "selection_id": "source-x:player-a",
        "decimal_odds": odds,
        "observed_ts": "2026-01-01T00:00:01+00:00",
        "source_id": "source-x",
        "sequence": max(position, 1),
        "market_type": "winner",
        "status": "open",
        "source_ts": "2026-01-01T00:00:00+00:00",
        "ingest_ts": "2026-01-01T00:00:01+00:00",
        "metadata": {},
        "score_state": None,
        "sport": "table_tennis",
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="source-x",
        lawful_terms_ref="terms:source-x:v1",
        retention_ref="retention:source-x:v1",
        stream_epoch=epoch,
        source_cursor=str(position),
        cursor_position=position,
        event_dedupe_key=MarketEvent.from_dict(payload).dedupe_key,
        event_id=payload["event_id"],
        source_payload_digest=digest_source_payload(raw),
        canonical_event_digest=canonical_event_digest(payload),
        source_observed_at="2026-01-01T00:00:01+00:00",
        collector_received_at="2026-01-01T00:00:02+00:00",
        collector_committed_at="2026-01-01T00:00:03+00:00",
        desktop_available_at="2026-01-01T00:00:04+00:00",
        revision_of=revision_of,
        revision_number=revision_number,
        gap_state=GapState.NONE,
        sync_state=sync_state,
    )


def acknowledge(store: DesktopDeltaCheckpointStore, delta: CollectorDelta) -> None:
    receipt = DesktopApplicationReceipt(
        delta_id=delta.delta_id,
        canonical_event_digest=delta.canonical_event_digest,
        receipt_id=f"receipt:{delta.delta_id}",
        applied_at="2026-01-01T00:00:05+00:00",
    )
    store.ack(
        delta,
        application_receipt=receipt,
        acknowledged_at="2026-01-01T00:00:06+00:00",
    )


class CollectorRetentionCompactionTests(unittest.TestCase):
    def make_history(self, root: str):
        path = Path(root) / "collector.sqlite"
        collector = CollectorDeltaStore(path)
        first = make_delta(delta_id="d1", position=1)
        terminal = make_delta(delta_id="d2", position=2)
        next_epoch = make_delta(
            delta_id="e2-d0",
            position=0,
            epoch="epoch-2",
            sync_state=SyncState.EPOCH_CHANGED,
        )
        self.assertTrue(collector.append(first))
        self.assertTrue(collector.append(terminal))
        self.assertTrue(collector.append(next_epoch))
        collector._record_runtime_stream_epoch_from_service(
            source_id="source-x",
            stream_epoch="epoch-1",
            activated_at="2026-01-01T00:00:08+00:00",
        )
        collector._record_runtime_stream_epoch_from_service(
            source_id="source-x",
            stream_epoch="epoch-2",
            activated_at="2026-01-01T00:00:09+00:00",
        )
        desktop = DesktopDeltaCheckpointStore(Path(root) / "desktop.json")
        return collector, desktop, first, terminal, next_epoch

    def test_acknowledged_inactive_epoch_compacts_but_keeps_checkpoint_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal, next_epoch = self.make_history(tmp)
            acknowledge(desktop, first)
            acknowledge(desktop, terminal)
            manager = CollectorRetentionManager(collector)

            plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            self.assertEqual(plan.delete_delta_ids, ("d1",))
            self.assertEqual(plan.retained_delta_ids, ("d2",))
            self.assertEqual(plan.current_stream_epoch, "epoch-2")
            self.assertEqual(plan.active_epoch_generation, 2)
            self.assertEqual(plan.terminal_checkpoint_delta_id, "d2")
            self.assertEqual(plan.desktop_transport_anchor_delta_id, "d2")

            result = manager.compact(
                plan,
                desktop_checkpoint=desktop,
                compacted_at="2026-01-02T00:00:00+00:00",
            )
            self.assertEqual(result.deleted_delta_ids, ("d1",))
            self.assertFalse(result.recovered_existing_journal)
            self.assertTrue(result.reclamation_complete)
            self.assertIsNone(collector.get(first.delta_id))
            self.assertEqual(collector.get(terminal.delta_id), terminal)
            self.assertEqual(collector.get(next_epoch.delta_id), next_epoch)
            self.assertEqual(
                collector.stream_checkpoint("source-x", "epoch-1").last_delta_id,
                "d2",
            )
            self.assertLessEqual(result.bytes_after, result.bytes_before)
            journal = manager.compaction_journal()
            self.assertEqual(journal[0]["plan_id"], plan.plan_id)
            self.assertTrue(journal[0]["reclamation_complete"])

            reopened = CollectorDeltaStore(Path(tmp) / "collector.sqlite")
            self.assertEqual(
                reopened.stream_checkpoint("source-x", "epoch-1").last_delta_id,
                "d2",
            )
            self.assertEqual(
                [item.delta_id for item in reopened.deltas_after_commit(source_id="source-x")],
                ["d2", "e2-d0"],
            )

    def test_unacknowledged_history_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal, _ = self.make_history(tmp)
            acknowledge(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            self.assertEqual(plan.delete_delta_ids, ())
            self.assertIn(first.delta_id, plan.unacknowledged_delta_ids)

    def test_durable_decision_pin_survives_restart_and_blocks_compaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal, _ = self.make_history(tmp)
            acknowledge(desktop, first)
            acknowledge(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            self.assertTrue(
                manager.pin(
                    kind=RetentionPinKind.DECISION,
                    owner_id="decision:42",
                    delta_id=first.delta_id,
                    canonical_event_digest=first.canonical_event_digest,
                    created_at="2026-01-01T00:00:07+00:00",
                )
            )

            reopened = CollectorDeltaStore(Path(tmp) / "collector.sqlite")
            manager = CollectorRetentionManager(reopened)
            pinned_plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            self.assertEqual(pinned_plan.delete_delta_ids, ())
            self.assertEqual(pinned_plan.pinned_delta_ids, ("d1",))

            self.assertTrue(
                manager.release_pin(
                    kind=RetentionPinKind.DECISION,
                    owner_id="decision:42",
                    delta_id=first.delta_id,
                    canonical_event_digest=first.canonical_event_digest,
                )
            )
            released_plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            self.assertEqual(released_plan.delete_delta_ids, ("d1",))

    def test_new_pin_invalidates_preview_before_any_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal, _ = self.make_history(tmp)
            acknowledge(desktop, first)
            acknowledge(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            manager.pin(
                kind=RetentionPinKind.REPLAY,
                owner_id="replay:run-7",
                delta_id=first.delta_id,
                canonical_event_digest=first.canonical_event_digest,
                created_at="2026-01-01T00:00:07+00:00",
            )
            with self.assertRaises(CollectorRetentionPlanStaleError):
                manager.compact(
                    plan,
                    desktop_checkpoint=desktop,
                    compacted_at="2026-01-02T00:00:00+00:00",
                )
            self.assertEqual(collector.get(first.delta_id), first)

    def test_retention_requires_product_owned_active_epoch_witness(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.sqlite")
            first = make_delta(delta_id="d1", position=1)
            terminal = make_delta(delta_id="d2", position=2)
            next_epoch = make_delta(
                delta_id="e2-d0",
                position=0,
                epoch="epoch-2",
                sync_state=SyncState.EPOCH_CHANGED,
            )
            for delta in (first, terminal, next_epoch):
                collector.append(delta)
            desktop = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            acknowledge(desktop, first)
            acknowledge(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            with self.assertRaisesRegex(
                CollectorRetentionError,
                "no product-owned active stream epoch",
            ):
                manager.preview(
                    source_id="source-x",
                    stream_epoch="epoch-1",
                    desktop_checkpoint=desktop,
                )
            self.assertEqual(manager.compaction_journal(), ())

    def test_current_epoch_is_product_resolved_and_cannot_be_faked(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal, next_epoch = self.make_history(tmp)
            acknowledge(desktop, first)
            acknowledge(desktop, terminal)
            acknowledge(desktop, next_epoch)
            manager = CollectorRetentionManager(collector)

            # The active epoch comes from the service-owned durable activation
            # journal, not retained delta ordering or a caller assertion.
            with self.assertRaisesRegex(
                CollectorRetentionError,
                "current collector stream epoch cannot be compacted",
            ):
                manager.preview(
                    source_id="source-x",
                    stream_epoch="epoch-2",
                    desktop_checkpoint=desktop,
                )

            # The old caller-authored authority parameter no longer exists. A stale
            # or fabricated assertion cannot mint a deletion plan.
            with self.assertRaises(TypeError):
                manager.preview(
                    source_id="source-x",
                    stream_epoch="epoch-2",
                    current_stream_epoch="fabricated-epoch",  # type: ignore[call-arg]
                    desktop_checkpoint=desktop,
                )
            self.assertEqual(manager.compaction_journal(), ())
            self.assertEqual(collector.get(next_epoch.delta_id), next_epoch)

    def test_runtime_reactivation_between_preview_and_apply_blocks_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal, _ = self.make_history(tmp)
            acknowledge(desktop, first)
            acknowledge(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            old_plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            self.assertEqual(old_plan.current_stream_epoch, "epoch-2")
            self.assertEqual(old_plan.active_epoch_generation, 2)

            generation = collector._record_runtime_stream_epoch_from_service(
                source_id="source-x",
                stream_epoch="epoch-1",
                activated_at="2026-01-01T00:00:10+00:00",
            )
            self.assertEqual(generation, 3)
            self.assertEqual(
                collector.runtime_stream_epoch("source-x"),
                ("epoch-1", 3),
            )
            with self.assertRaisesRegex(
                CollectorRetentionError,
                "current collector stream epoch cannot be compacted",
            ):
                manager.compact(
                    old_plan,
                    desktop_checkpoint=desktop,
                    compacted_at="2026-01-02T00:00:00+00:00",
                )
            self.assertEqual(collector.get(first.delta_id), first)
            self.assertEqual(manager.compaction_journal(), ())

            # Even if the current epoch returns to the same value as preview, the
            # monotonic generation makes the ABA transition stale.
            generation = collector._record_runtime_stream_epoch_from_service(
                source_id="source-x",
                stream_epoch="epoch-2",
                activated_at="2026-01-01T00:00:11+00:00",
            )
            self.assertEqual(generation, 4)
            with self.assertRaises(CollectorRetentionPlanStaleError):
                manager.compact(
                    old_plan,
                    desktop_checkpoint=desktop,
                    compacted_at="2026-01-02T00:00:00+00:00",
                )
            self.assertEqual(collector.get(first.delta_id), first)
            self.assertEqual(manager.compaction_journal(), ())

    def test_compacted_delta_identity_tombstone_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal, _ = self.make_history(tmp)
            acknowledge(desktop, first)
            acknowledge(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            manager.compact(
                plan,
                desktop_checkpoint=desktop,
                compacted_at="2026-01-02T00:00:00+00:00",
            )
            self.assertIsNone(collector.get(first.delta_id))

            reopened = CollectorDeltaStore(Path(tmp) / "collector.sqlite")
            self.assertFalse(reopened.append(first))
            conflicting = make_delta(
                delta_id="d1",
                position=1,
                odds="1.95",
            )
            with self.assertRaises(DeltaConflictError):
                reopened.append(conflicting)
            self.assertIsNone(reopened.get("d1"))

    def test_post_commit_vacuum_failure_is_terminal_and_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal, _ = self.make_history(tmp)
            acknowledge(desktop, first)
            acknowledge(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )

            with patch.object(
                manager,
                "_reclaim_pages",
                side_effect=sqlite3.OperationalError("injected vacuum failure"),
            ):
                first_result = manager.compact(
                    plan,
                    desktop_checkpoint=desktop,
                    compacted_at="2026-01-02T00:00:00+00:00",
                )
            self.assertFalse(first_result.recovered_existing_journal)
            self.assertFalse(first_result.reclamation_complete)
            self.assertIsNone(collector.get(first.delta_id))
            self.assertEqual(collector.get(terminal.delta_id), terminal)
            journal = manager.compaction_journal()
            self.assertEqual(len(journal), 1)
            self.assertFalse(journal[0]["reclamation_complete"])

            # Simulate restart after semantic commit but before physical page
            # reclamation. The exact original plan converges to that same terminal
            # journal and completes maintenance instead of becoming stale.
            reopened = CollectorDeltaStore(Path(tmp) / "collector.sqlite")
            recovered_manager = CollectorRetentionManager(reopened)
            recovered = recovered_manager.compact(
                plan,
                desktop_checkpoint=desktop,
                compacted_at="2026-01-03T00:00:00+00:00",
            )
            self.assertTrue(recovered.recovered_existing_journal)
            self.assertTrue(recovered.reclamation_complete)
            self.assertEqual(recovered.compacted_at, "2026-01-02T00:00:00+00:00")
            self.assertEqual(len(recovered_manager.compaction_journal()), 1)
            self.assertTrue(
                recovered_manager.compaction_journal()[0]["reclamation_complete"]
            )

    def test_retained_revision_keeps_its_predecessor(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector = CollectorDeltaStore(Path(tmp) / "collector.sqlite")
            first = make_delta(delta_id="d1", position=1)
            terminal = make_delta(delta_id="d2", position=2)
            correction = make_delta(
                delta_id="d1r",
                position=1,
                revision_of="d1",
                revision_number=1,
            )
            next_epoch = make_delta(
                delta_id="e2-d0",
                position=0,
                epoch="epoch-2",
                sync_state=SyncState.EPOCH_CHANGED,
            )
            for delta in (first, terminal, correction, next_epoch):
                collector.append(delta)
            collector._record_runtime_stream_epoch_from_service(
                source_id="source-x",
                stream_epoch="epoch-1",
                activated_at="2026-01-01T00:00:08+00:00",
            )
            collector._record_runtime_stream_epoch_from_service(
                source_id="source-x",
                stream_epoch="epoch-2",
                activated_at="2026-01-01T00:00:09+00:00",
            )
            desktop = DesktopDeltaCheckpointStore(Path(tmp) / "desktop.json")
            for delta in (first, terminal, correction):
                acknowledge(desktop, delta)
            manager = CollectorRetentionManager(collector)
            plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            self.assertEqual(plan.delete_delta_ids, ())
            self.assertIn("d1r", plan.retained_delta_ids)
            self.assertIn("d1", plan.retained_delta_ids)


if __name__ == "__main__":
    unittest.main()
