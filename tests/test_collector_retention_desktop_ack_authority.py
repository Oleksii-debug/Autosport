import json
import tempfile
import unittest
from pathlib import Path

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


def _activate(
    store: CollectorDeltaStore,
    *,
    stream_epoch: str,
    generation: int,
    activated_at: str,
) -> None:
    connection = store._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO collector_epoch_activations_v1("
            "source_id, generation, stream_epoch, activated_at"
            ") VALUES(?,?,?,?)",
            ("source-x", generation, stream_epoch, activated_at),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


class CollectorRetentionDesktopAckAuthorityTests(unittest.TestCase):
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
        self.assertTrue(collector.append(first))
        self.assertTrue(collector.append(terminal))
        self.assertTrue(collector.append(current))
        _activate(
            collector,
            stream_epoch="epoch-1",
            generation=1,
            activated_at="2026-01-01T00:00:08+00:00",
        )
        _activate(
            collector,
            stream_epoch="epoch-2",
            generation=2,
            activated_at="2026-01-01T00:00:09+00:00",
        )
        desktop = DesktopDeltaCheckpointStore(Path(root) / "desktop.json")
        return collector, desktop, first, terminal

    @staticmethod
    def _install_attack(
        desktop: DesktopDeltaCheckpointStore,
        calls: dict[str, int],
    ) -> None:
        def forged_has_ack(_delta_id: str) -> bool:
            calls["has_ack"] += 1
            return True

        def forged_receipt(delta: CollectorDelta) -> DesktopApplicationReceipt:
            calls["application_receipt"] += 1
            return DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=delta.canonical_event_digest,
                receipt_id=f"forged:{delta.delta_id}",
                applied_at="2026-01-01T00:00:05+00:00",
            )

        desktop.has_ack = forged_has_ack
        desktop.application_receipt = forged_receipt

    @staticmethod
    def _remove_attack(desktop: DesktopDeltaCheckpointStore) -> None:
        desktop.__dict__.pop("has_ack", None)
        desktop.__dict__.pop("application_receipt", None)

    def test_shadowed_exact_checkpoint_cannot_mint_preview_deletion_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            collector, desktop, first, terminal = self._history(tmp)
            _ack(desktop, terminal)
            manager = CollectorRetentionManager(collector)
            calls = {"has_ack": 0, "application_receipt": 0}
            self._install_attack(desktop, calls)

            with self.assertRaisesRegex(TypeError, "instance-shadowed"):
                manager.preview(
                    source_id="source-x",
                    stream_epoch="epoch-1",
                    desktop_checkpoint=desktop,
                )

            self.assertEqual(calls, {"has_ack": 0, "application_receipt": 0})
            self.assertEqual(collector.get(first.delta_id), first)
            self.assertEqual(manager.compaction_journal(), ())

            self._remove_attack(desktop)
            plan = manager.preview(
                source_id="source-x",
                stream_epoch="epoch-1",
                desktop_checkpoint=desktop,
            )
            self.assertEqual(plan.delete_delta_ids, ())
            self.assertIn(first.delta_id, plan.unacknowledged_delta_ids)
            reopened = CollectorDeltaStore(Path(tmp) / "collector.sqlite")
            self.assertEqual(reopened.get(first.delta_id), first)

    def test_shadow_added_after_preview_blocks_apply_before_any_delete(self):
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
            self._install_attack(desktop, calls)
            with self.assertRaisesRegex(TypeError, "instance-shadowed"):
                manager.compact(
                    plan,
                    desktop_checkpoint=desktop,
                    compacted_at="2026-01-02T00:00:00+00:00",
                )

            self.assertEqual(calls, {"has_ack": 0, "application_receipt": 0})
            self.assertEqual(collector.get(first.delta_id), first)
            self.assertEqual(manager.compaction_journal(), ())

            self._remove_attack(desktop)
            result = manager.compact(
                plan,
                desktop_checkpoint=desktop,
                compacted_at="2026-01-02T00:00:00+00:00",
            )
            self.assertEqual(result.deleted_delta_ids, (first.delta_id,))
            self.assertIsNone(collector.get(first.delta_id))


if __name__ == "__main__":
    unittest.main()
