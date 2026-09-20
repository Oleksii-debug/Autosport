import importlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport import _collector_retention_desktop_ack_authority as ack_guard
from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    GapState,
    SyncState,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.collector_retention import CollectorRetentionManager
from autosport.domain import MarketEvent


def _delta(
    *,
    delta_id: str,
    position: int,
    epoch: str = "epoch-1",
    sync_state: SyncState = SyncState.READY,
) -> CollectorDelta:
    payload = {
        "event_id": "source-x:event-1",
        "market_id": "source-x:winner",
        "selection_id": "source-x:player-a",
        "decimal_odds": "1.80",
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
        revision_of=None,
        revision_number=0,
        gap_state=GapState.NONE,
        sync_state=sync_state,
    )


def _ack(store: DesktopDeltaCheckpointStore, delta: CollectorDelta) -> None:
    store.ack(
        delta,
        application_receipt=DesktopApplicationReceipt(
            delta_id=delta.delta_id,
            canonical_event_digest=delta.canonical_event_digest,
            receipt_id=f"receipt:{delta.delta_id}",
            applied_at="2026-01-01T00:00:05+00:00",
        ),
        acknowledged_at="2026-01-01T00:00:06+00:00",
    )


def _activate(store: CollectorDeltaStore, *, epoch: str, generation: int) -> None:
    connection = store._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO collector_epoch_activations_v1("
            "source_id, generation, stream_epoch, activated_at"
            ") VALUES(?,?,?,?)",
            (
                "source-x",
                generation,
                epoch,
                f"2026-01-01T00:00:{7 + generation:02d}+00:00",
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


class CollectorRetentionDesktopAckClassAuthorityTests(unittest.TestCase):
    def _history(self, root: str):
        collector = CollectorDeltaStore(Path(root) / "collector.sqlite")
        first = _delta(delta_id="d1", position=1)
        terminal = _delta(delta_id="d2", position=2)
        current = _delta(
            delta_id="e2-d0",
            position=0,
            epoch="epoch-2",
            sync_state=SyncState.EPOCH_CHANGED,
        )
        for delta in (first, terminal, current):
            self.assertTrue(collector.append(delta))
        _activate(collector, epoch="epoch-1", generation=1)
        _activate(collector, epoch="epoch-2", generation=2)
        desktop = DesktopDeltaCheckpointStore(Path(root) / "desktop.json")
        return collector, desktop, first, terminal

    @staticmethod
    def _install_class_attack(calls: dict[str, int]):
        original_has_ack = DesktopDeltaCheckpointStore.has_ack
        original_receipt = DesktopDeltaCheckpointStore.application_receipt

        def forged_has_ack(self, _delta_id: str) -> bool:
            calls["has_ack"] += 1
            return True

        def forged_receipt(self, delta: CollectorDelta) -> DesktopApplicationReceipt:
            calls["application_receipt"] += 1
            return DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id=f"forged:{delta.delta_id}",
                applied_at="2026-01-01T00:00:05+00:00",
            )

        DesktopDeltaCheckpointStore.has_ack = forged_has_ack
        DesktopDeltaCheckpointStore.application_receipt = forged_receipt
        return original_has_ack, original_receipt

    @staticmethod
    def _restore_class(original_has_ack, original_receipt) -> None:
        DesktopDeltaCheckpointStore.has_ack = original_has_ack
        DesktopDeltaCheckpointStore.application_receipt = original_receipt

    def test_alternate_exact_checkpoint_cannot_mint_deletion_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal = self._history(tmp)
            _ack(desktop, terminal)
            alternate = DesktopDeltaCheckpointStore(Path(tmp) / "alternate-desktop.json")
            _ack(alternate, first)
            _ack(alternate, terminal)
            manager = CollectorRetentionManager(collector)

            with self.assertRaisesRegex(TypeError, "exactly one canonical"):
                manager.preview(
                    source_id="source-x",
                    stream_epoch="epoch-1",
                    desktop_checkpoint=alternate,
                )

            self.assertEqual(collector.get(first.delta_id), first)
            self.assertEqual(manager.compaction_journal(), ())

    def test_checkpoint_path_substitution_after_preview_blocks_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal = self._history(tmp)
            _ack(desktop, first)
            _ack(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            self.assertEqual(plan.delete_delta_ids, (first.delta_id,))

            original_path = desktop.path
            alternate = DesktopDeltaCheckpointStore(Path(tmp) / "alternate" / "desktop.json")
            _ack(alternate, first)
            _ack(alternate, terminal)
            desktop.path = alternate.path
            try:
                with self.assertRaisesRegex(TypeError, "product-owned canonical"):
                    manager.compact(
                        plan,
                        desktop_checkpoint=desktop,
                        compacted_at="2026-01-02T00:00:00+00:00",
                    )
                self.assertEqual(collector.get(first.delta_id), first)
                self.assertEqual(manager.compaction_journal(), ())
            finally:
                desktop.path = original_path

            result = manager.compact(
                plan,
                desktop_checkpoint=desktop,
                compacted_at="2026-01-02T00:00:00+00:00",
            )
            self.assertEqual(result.deleted_delta_ids, (first.delta_id,))
            self.assertIsNone(collector.get(first.delta_id))

    def test_class_rebind_cannot_mint_preview_deletion_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal = self._history(tmp)
            _ack(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            calls = {"has_ack": 0, "application_receipt": 0}
            originals = self._install_class_attack(calls)
            try:
                with self.assertRaisesRegex(TypeError, "class-rebound"):
                    manager.preview(
                        source_id="source-x",
                        stream_epoch="epoch-1",
                        desktop_checkpoint=desktop,
                    )
                self.assertEqual(calls, {"has_ack": 0, "application_receipt": 0})
                self.assertEqual(collector.get(first.delta_id), first)
                self.assertEqual(manager.compaction_journal(), ())
            finally:
                self._restore_class(*originals)

    def test_class_rebind_after_preview_blocks_apply_before_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal = self._history(tmp)
            _ack(desktop, first)
            _ack(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            self.assertEqual(plan.delete_delta_ids, (first.delta_id,))

            calls = {"has_ack": 0, "application_receipt": 0}
            originals = self._install_class_attack(calls)
            try:
                with self.assertRaisesRegex(TypeError, "class-rebound"):
                    manager.compact(
                        plan,
                        desktop_checkpoint=desktop,
                        compacted_at="2026-01-02T00:00:00+00:00",
                    )
                self.assertEqual(calls, {"has_ack": 0, "application_receipt": 0})
                self.assertEqual(collector.get(first.delta_id), first)
                self.assertEqual(manager.compaction_journal(), ())
            finally:
                self._restore_class(*originals)

    def test_guard_reload_does_not_recapture_rebound_class_methods(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal = self._history(tmp)
            _ack(desktop, terminal)
            calls = {"has_ack": 0, "application_receipt": 0}
            originals = self._install_class_attack(calls)
            try:
                importlib.reload(ack_guard)
                manager = CollectorRetentionManager(collector)
                with self.assertRaisesRegex(TypeError, "class-rebound"):
                    manager.preview(
                        source_id="source-x",
                        stream_epoch="epoch-1",
                        desktop_checkpoint=desktop,
                    )
                self.assertEqual(calls, {"has_ack": 0, "application_receipt": 0})
                self.assertEqual(collector.get(first.delta_id), first)
                self.assertEqual(manager.compaction_journal(), ())
            finally:
                self._restore_class(*originals)
                importlib.reload(ack_guard)

            # Reload after restoration must continue to dispatch to the original
            # unwrapped build-plan implementation rather than recursively wrapping
            # the prior guard installation.
            plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            self.assertEqual(plan.delete_delta_ids, ())
            self.assertIn(first.delta_id, plan.unacknowledged_delta_ids)


if __name__ == "__main__":
    unittest.main()
