import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    CursorRegressionError,
    DeltaConflictError,
    GapState,
    StreamCheckpoint,
    SyncState,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.domain import MarketEvent


def event_payload(*, odds="1.80"):
    return {
        "event_id": "e1",
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


def make_delta(
    *,
    delta_id="d1",
    cursor_position=1,
    payload=None,
    revision_of=None,
    revision_number=0,
):
    payload = payload or event_payload()
    source_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="source-x",
        lawful_terms_ref="terms:source-x:v1",
        retention_ref="retention:source-x:v1",
        stream_epoch="epoch-1",
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
        revision_of=revision_of,
        revision_number=revision_number,
        gap_state=GapState.NONE,
        sync_state=SyncState.READY,
    )


class CollectorSQLiteStoreTests(unittest.TestCase):
    def test_new_store_is_sqlite_and_reopens_without_changing_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            store = CollectorDeltaStore(path)
            delta = make_delta()
            self.assertTrue(store.append(delta))
            self.assertEqual(path.read_bytes()[:16], b"SQLite format 3\x00")
            reopened = CollectorDeltaStore(path)
            self.assertEqual(reopened.get(delta.delta_id), delta)
            checkpoint = reopened.stream_checkpoint("source-x", "epoch-1")
            self.assertEqual(checkpoint.last_delta_id, delta.delta_id)

    def test_legacy_json_migrates_once_and_preserves_exact_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            delta = make_delta()
            checkpoint = StreamCheckpoint(
                source_id="source-x",
                stream_epoch="epoch-1",
                last_cursor="1",
                last_position=1,
                last_delta_id="d1",
            )
            raw = {
                "schema_version": 1,
                "deltas": [delta.to_dict()],
                "streams": {"source-x|epoch-1": asdict(checkpoint)},
            }
            source = json.dumps(
                raw,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            ) + "\n"
            path.write_text(source, encoding="utf-8")

            migrated = CollectorDeltaStore(path)

            self.assertEqual(migrated.get("d1"), delta)
            self.assertEqual(path.read_bytes()[:16], b"SQLite format 3\x00")
            self.assertEqual(
                path.with_name("collector.json.legacy-v1.json").read_text(encoding="utf-8"),
                source,
            )
            self.assertFalse(CollectorDeltaStore(path).append(delta))

    def test_legacy_stream_checkpoint_mismatch_fails_before_authority_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            delta = make_delta()
            raw = {
                "schema_version": 1,
                "deltas": [delta.to_dict()],
                "streams": {
                    "source-x|epoch-1": {
                        "source_id": "source-x",
                        "stream_epoch": "epoch-1",
                        "last_cursor": "9",
                        "last_position": 9,
                        "last_delta_id": "d1",
                    }
                },
            }
            source = json.dumps(raw, sort_keys=True) + "\n"
            path.write_text(source, encoding="utf-8")
            with self.assertRaises(ValueError):
                CollectorDeltaStore(path)
            self.assertEqual(path.read_text(encoding="utf-8"), source)

    def test_late_correction_delivery_uses_commit_order_not_source_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            first = make_delta(delta_id="d1", cursor_position=1)
            second = make_delta(delta_id="d2", cursor_position=2)
            correction = make_delta(
                delta_id="d1r",
                cursor_position=1,
                payload=event_payload(odds="1.95"),
                revision_of="d1",
                revision_number=1,
            )
            store.append(first)
            store.append(second)
            store.append(correction)
            self.assertEqual(
                [item.delta_id for item in store.deltas_after_commit(source_id="source-x")],
                ["d1", "d2", "d1r"],
            )
            self.assertEqual(
                [
                    item.delta_id
                    for item in store.deltas_after_commit(
                        source_id="source-x", after_delta_id="d2"
                    )
                ],
                ["d1r"],
            )
            self.assertEqual(
                store.stream_checkpoint("source-x", "epoch-1").last_delta_id,
                "d2",
            )

    def test_global_duplicate_identity_is_durable_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            original = make_delta()
            CollectorDeltaStore(path).append(original)
            conflict = replace(
                original,
                canonical_event_digest="f" * 64,
            )
            with self.assertRaises(DeltaConflictError):
                CollectorDeltaStore(path).append(conflict)

    def test_payload_digest_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            delta = make_delta()
            CollectorDeltaStore(path).append(delta)
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE collector_deltas SET payload_json=? WHERE delta_id=?",
                    ("{}", delta.delta_id),
                )
            with self.assertRaises(ValueError):
                CollectorDeltaStore(path).get(delta.delta_id)

    def test_concurrent_same_delta_admission_is_exactly_once_without_sleep(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            CollectorDeltaStore(path)
            delta = make_delta()
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def worker():
                try:
                    store = CollectorDeltaStore(path)
                    barrier.wait()
                    results.append(store.append(delta))
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(CollectorDeltaStore(path).get("d1"), delta)

    def test_delivery_anchor_must_belong_to_requested_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.json")
            delta = make_delta()
            store.append(delta)
            with self.assertRaises(CursorRegressionError):
                store.deltas_after_commit(
                    source_id="other-source",
                    after_delta_id=delta.delta_id,
                )


if __name__ == "__main__":
    unittest.main()
