import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import autosport.live_observation as live_observation_module
import autosport.storage as storage_module
from autosport.domain import MarketEvent
from autosport.ingestion_health import IngestionPolicy, SourceHealthStore
from autosport.live_observation import (
    OneShotObservationWorker,
    observe_workspace_once,
    poll_open_market_store_once,
)
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import BoundedMirrorInvalidationBuffer
from autosport.providers import InMemoryProvider, ProviderBatch, ProviderQuote
from autosport.storage import SQLiteMarketStore
from autosport.ui_model import observation_quote_lines, observation_summary


_RECEIVE_TIME = "2026-09-12T20:00:02+00:00"


class LiveObservationTests(unittest.TestCase):
    @staticmethod
    def _provider() -> InMemoryProvider:
        return InMemoryProvider(
            "live-fixture",
            [
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="player-a",
                    decimal_odds=Decimal("1.80"),
                    observed_ts="2026-09-12T20:00:00+00:00",
                    sequence=1,
                    source_ts="2026-09-12T19:59:59+00:00",
                ),
                ProviderQuote(
                    provider_event_id="match-1",
                    provider_market_id="winner",
                    provider_selection_id="player-b",
                    decimal_odds=Decimal("2.05"),
                    observed_ts="2026-09-12T20:00:00+00:00",
                    sequence=2,
                    source_ts="2026-09-12T19:59:59+00:00",
                ),
            ],
        )

    @staticmethod
    def _observe(workspace: str | Path):
        return observe_workspace_once(
            workspace,
            LiveObservationTests._provider(),
            max_items=10,
            clock=lambda: _RECEIVE_TIME,
        )

    @staticmethod
    def _wait_for_message(worker: OneShotObservationWorker, timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = worker.poll()
            if message is not None:
                return message
            time.sleep(0.01)
        raise AssertionError("worker did not publish terminal message")

    def test_workspace_observer_uses_short_lived_market_and_health_stores_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._observe(tmp)
            self.assertEqual(result.stats.accepted, 2)
            self.assertEqual(result.health.status, "healthy")
            self.assertEqual(len(result.current_quotes), 2)
            self.assertTrue((Path(tmp) / "market.db").exists())
            self.assertTrue((Path(tmp) / "source_health.json").exists())
            self.assertFalse((Path(tmp) / "paper_book.json").exists())

    def test_workspace_observer_closes_market_store_if_health_store_init_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("autosport.live_observation.SQLiteMarketStore") as store_type:
                with patch(
                    "autosport.live_observation.SourceHealthStore",
                    side_effect=OSError("health-store-init-failed"),
                ):
                    with self.assertRaisesRegex(OSError, "health-store-init-failed"):
                        observe_workspace_once(
                            tmp,
                            self._provider(),
                            max_items=10,
                            clock=lambda: _RECEIVE_TIME,
                        )

            store_type.return_value.close.assert_called_once_with()

    def test_live_observation_rejects_substituted_authority_types_before_provider_read(self):
        class Store(SQLiteMarketStore):
            pass

        class HealthStore(SourceHealthStore):
            pass

        class Updates(BoundedMirrorInvalidationBuffer):
            pass

        class ExplosiveProvider:
            source_id = "live-fixture"

            def read_batch(self, max_items: int = 1000):
                raise AssertionError("provider read executed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            health_store = SourceHealthStore(root / "source_health.json")
            updates = BoundedMirrorInvalidationBuffer(MarketMirror.from_store(store))
            hostile_store = Store(root / "other-market.db")
            hostile_health = HealthStore(root / "other-health.json")
            hostile_updates = Updates(MarketMirror())
            try:
                with self.assertRaisesRegex(TypeError, "exact SQLiteMarketStore"):
                    poll_open_market_store_once(
                        hostile_store,
                        health_store,
                        ExplosiveProvider(),
                        mirror_updates=updates,
                    )
                with self.assertRaisesRegex(TypeError, "exact SourceHealthStore"):
                    poll_open_market_store_once(
                        store,
                        hostile_health,
                        ExplosiveProvider(),
                        mirror_updates=updates,
                    )
                with self.assertRaisesRegex(
                    TypeError,
                    "exact BoundedMirrorInvalidationBuffer",
                ):
                    poll_open_market_store_once(
                        store,
                        health_store,
                        ExplosiveProvider(),
                        mirror_updates=hostile_updates,
                    )
                observer_root = root / "workspace-observer"
                with self.assertRaisesRegex(
                    TypeError,
                    "exact BoundedMirrorInvalidationBuffer",
                ):
                    observe_workspace_once(
                        observer_root,
                        ExplosiveProvider(),
                        mirror_updates=hostile_updates,
                    )
                self.assertFalse(observer_root.exists())
            finally:
                hostile_store.close()
                store.close()

    def test_workspace_observer_rejects_invalid_max_items_before_workspace_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "invalid-max-items"

            with self.assertRaisesRegex(ValueError, "positive integer"):
                observe_workspace_once(
                    root,
                    self._provider(),
                    max_items=0,
                    clock=lambda: _RECEIVE_TIME,
                )

            self.assertFalse(root.exists())

    def test_workspace_observer_rejects_policy_subclass_before_workspace_creation(self):
        class Policy(IngestionPolicy):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "invalid-policy"

            with self.assertRaisesRegex(TypeError, "exact IngestionPolicy"):
                observe_workspace_once(
                    root,
                    self._provider(),
                    max_items=10,
                    policy=Policy(),
                    clock=lambda: _RECEIVE_TIME,
                )

            self.assertFalse(root.exists())

    def test_workspace_observer_rejects_noncallable_clock_before_workspace_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "invalid-clock"

            with self.assertRaisesRegex(TypeError, "clock must be callable"):
                observe_workspace_once(
                    root,
                    self._provider(),
                    max_items=10,
                    clock="not-a-clock",
                )

            self.assertFalse(root.exists())

    def test_workspace_observer_rejects_provider_identity_before_workspace_creation(self):
        class Text(str):
            pass

        class Provider:
            source_id = Text("live-fixture")

            def read_batch(self, max_items: int = 1000):
                raise AssertionError("provider read executed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "invalid-provider"

            with self.assertRaisesRegex(TypeError, "exact string"):
                observe_workspace_once(
                    root,
                    Provider(),
                    max_items=10,
                    clock=lambda: _RECEIVE_TIME,
                )

            self.assertFalse(root.exists())

    def test_workspace_observer_rejects_backpressure_overflow_before_workspace_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "invalid-backpressure"
            policy = IngestionPolicy(max_batch_size=1)

            with self.assertRaisesRegex(ValueError, "exceeds backpressure limit"):
                observe_workspace_once(
                    root,
                    self._provider(),
                    max_items=2,
                    policy=policy,
                    clock=lambda: _RECEIVE_TIME,
                )

            self.assertFalse(root.exists())

    def test_open_store_poll_rejects_invalid_controls_before_provider_read(self):
        class ExplosiveProvider:
            source_id = "live-fixture"

            def read_batch(self, max_items: int = 1000):
                raise AssertionError("provider read executed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                updates = BoundedMirrorInvalidationBuffer(MarketMirror.from_store(store))
                health_store = SourceHealthStore(root / "source_health.json")
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    poll_open_market_store_once(
                        store,
                        health_store,
                        ExplosiveProvider(),
                        mirror_updates=updates,
                        max_items=0,
                    )
                with self.assertRaisesRegex(TypeError, "clock must be callable"):
                    poll_open_market_store_once(
                        store,
                        health_store,
                        ExplosiveProvider(),
                        mirror_updates=updates,
                        max_items=1,
                        clock="not-a-clock",
                    )
                class Policy(IngestionPolicy):
                    pass
                with self.assertRaisesRegex(TypeError, "exact IngestionPolicy"):
                    poll_open_market_store_once(
                        store,
                        health_store,
                        ExplosiveProvider(),
                        mirror_updates=updates,
                        max_items=1,
                        policy=Policy(),
                    )
                with self.assertRaisesRegex(ValueError, "exceeds backpressure limit"):
                    poll_open_market_store_once(
                        store,
                        health_store,
                        ExplosiveProvider(),
                        mirror_updates=updates,
                        max_items=2,
                        policy=IngestionPolicy(max_batch_size=1),
                    )
                self.assertEqual(store.events(), ())
                self.assertEqual(updates.mirror.view().events, ())
            finally:
                store.close()

    def test_open_store_poll_rejects_batch_source_identity_mismatch_before_persistence(self):
        class MismatchedBatchProvider:
            source_id = "live-fixture"

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                return ProviderBatch(
                    source_id="foreign-source",
                    quotes=(
                        ProviderQuote(
                            provider_event_id="match-1",
                            provider_market_id="winner",
                            provider_selection_id="player-a",
                            decimal_odds=Decimal("1.80"),
                            observed_ts="2026-09-12T20:00:00+00:00",
                            sequence=1,
                            source_ts="2026-09-12T19:59:59+00:00",
                        ),
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                updates = BoundedMirrorInvalidationBuffer(MarketMirror.from_store(store))
                health_store = SourceHealthStore(root / "source_health.json")
                with self.assertRaisesRegex(
                    RuntimeError,
                    "batch source identity conflicts",
                ):
                    poll_open_market_store_once(
                        store,
                        health_store,
                        MismatchedBatchProvider(),
                        mirror_updates=updates,
                        max_items=10,
                        clock=lambda: _RECEIVE_TIME,
                    )
                self.assertEqual(store.events(), ())
                self.assertEqual(updates.mirror.view().events, ())
            finally:
                store.close()

    def test_open_store_poll_rejects_provider_source_id_subclass_before_read(self):
        class Text(str):
            pass

        class Provider:
            source_id = Text("live-fixture")

            def read_batch(self, max_items: int = 1000):
                raise AssertionError("provider read executed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                updates = BoundedMirrorInvalidationBuffer(MarketMirror.from_store(store))
                health_store = SourceHealthStore(root / "source_health.json")
                with self.assertRaisesRegex(TypeError, "exact string"):
                    poll_open_market_store_once(
                        store,
                        health_store,
                        Provider(),
                        mirror_updates=updates,
                        max_items=10,
                        clock=lambda: _RECEIVE_TIME,
                    )
                self.assertEqual(store.events(), ())
                self.assertEqual(updates.mirror.view().events, ())
            finally:
                store.close()

    def test_open_store_poll_rejects_substituted_batch_before_batch_property_access(self):
        class HostileBatch:
            @property
            def source_id(self):
                raise AssertionError("hostile batch source_id accessed")

        class Provider:
            source_id = "live-fixture"

            def read_batch(self, max_items: int = 1000):
                return HostileBatch()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                updates = BoundedMirrorInvalidationBuffer(MarketMirror.from_store(store))
                health_store = SourceHealthStore(root / "source_health.json")
                with self.assertRaisesRegex(TypeError, "exact ProviderBatch"):
                    poll_open_market_store_once(
                        store,
                        health_store,
                        Provider(),
                        mirror_updates=updates,
                        max_items=10,
                        clock=lambda: _RECEIVE_TIME,
                    )
                self.assertEqual(store.events(), ())
                self.assertEqual(updates.mirror.view().events, ())
            finally:
                store.close()

    def test_open_store_poll_rejects_equal_string_subclass_source_substitution(self):
        class SourceId(str):
            pass

        class MutatingProvider:
            def __init__(self) -> None:
                self.source_id = "live-fixture"

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                self.source_id = SourceId("live-fixture")
                return ProviderBatch(
                    source_id="live-fixture",
                    quotes=(),
                    cursor="cursor-1",
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                updates = BoundedMirrorInvalidationBuffer(MarketMirror.from_store(store))
                health_store = SourceHealthStore(root / "source_health.json")
                with self.assertRaisesRegex(
                    RuntimeError,
                    "source identity changed during live batch read",
                ):
                    poll_open_market_store_once(
                        store,
                        health_store,
                        MutatingProvider(),
                        mirror_updates=updates,
                        max_items=10,
                        clock=lambda: _RECEIVE_TIME,
                    )
                self.assertEqual(store.events(), ())
                self.assertEqual(updates.mirror.view().events, ())
            finally:
                store.close()

    def test_open_store_poll_rejects_provider_source_mutation_during_read_before_persistence(self):
        class MutatingProvider:
            def __init__(self) -> None:
                self.source_id = "live-fixture"

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                self.source_id = "mutated-source"
                return ProviderBatch(
                    source_id="live-fixture",
                    quotes=(
                        ProviderQuote(
                            provider_event_id="match-1",
                            provider_market_id="winner",
                            provider_selection_id="player-a",
                            decimal_odds=Decimal("1.80"),
                            observed_ts="2026-09-12T20:00:00+00:00",
                            sequence=1,
                            source_ts="2026-09-12T19:59:59+00:00",
                        ),
                    ),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                updates = BoundedMirrorInvalidationBuffer(MarketMirror.from_store(store))
                health_store = SourceHealthStore(root / "source_health.json")
                with self.assertRaisesRegex(
                    RuntimeError,
                    "source identity changed during live batch read",
                ):
                    poll_open_market_store_once(
                        store,
                        health_store,
                        MutatingProvider(),
                        mirror_updates=updates,
                        max_items=10,
                        clock=lambda: _RECEIVE_TIME,
                    )
                self.assertEqual(store.events(), ())
                self.assertEqual(updates.mirror.view().events, ())
            finally:
                store.close()

    def test_sqlite_retry_preserves_primary_failure_when_provider_reset_fails(self):
        batch = ProviderBatch(
            source_id="live-fixture",
            quotes=(),
            cursor="cursor-1",
            quality_flags=("TRUNCATED_BATCH",),
        )

        class Provider:
            source_id = "live-fixture"

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                return batch

            def reset_pending_snapshot(self) -> None:
                raise RuntimeError("reset-failed")

        class Engine:
            def poll_once(self, provider, max_items: int = 1000):
                provider.read_batch(max_items=max_items)
                raise sqlite3.OperationalError("database-locked")

        wrapped = live_observation_module._ReplayableBatchProvider(Provider())
        with self.assertRaisesRegex(sqlite3.OperationalError, "database-locked") as raised:
            live_observation_module._poll_acknowledged(
                Engine(),
                wrapped,
                max_items=1,
            )

        self.assertFalse(wrapped.has_inflight)
        self.assertTrue(
            any(
                "provider pending-snapshot reset also failed: RuntimeError: reset-failed"
                in note
                for note in getattr(raised.exception, "__notes__", ())
            )
        )

    def test_sqlite_retry_preserves_primary_failure_when_provider_reset_is_unprintable(self):
        batch = ProviderBatch(
            source_id="live-fixture",
            quotes=(),
            cursor="cursor-1",
            quality_flags=("TRUNCATED_BATCH",),
        )

        class UnprintableResetError(RuntimeError):
            def __str__(self) -> str:
                raise RuntimeError("stringification-failed")

        class Provider:
            source_id = "live-fixture"

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                return batch

            def reset_pending_snapshot(self) -> None:
                raise UnprintableResetError()

        class Engine:
            def poll_once(self, provider, max_items: int = 1000):
                provider.read_batch(max_items=max_items)
                raise sqlite3.OperationalError("database-locked")

        wrapped = live_observation_module._ReplayableBatchProvider(Provider())
        with self.assertRaisesRegex(sqlite3.OperationalError, "database-locked") as raised:
            live_observation_module._poll_acknowledged(
                Engine(),
                wrapped,
                max_items=1,
            )

        self.assertFalse(wrapped.has_inflight)
        self.assertTrue(
            any(
                "provider pending-snapshot reset also failed: "
                "UnprintableResetError: <unprintable exception>" in note
                for note in getattr(raised.exception, "__notes__", ())
            )
        )

    def test_sqlite_retry_preserves_primary_failure_when_provider_reset_raises_base_exception(self):
        batch = ProviderBatch(
            source_id="live-fixture",
            quotes=(),
            cursor="cursor-1",
            quality_flags=("TRUNCATED_BATCH",),
        )

        class Provider:
            source_id = "live-fixture"

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                return batch

            def reset_pending_snapshot(self) -> None:
                raise SystemExit("reset-stop")

        class Engine:
            def poll_once(self, provider, max_items: int = 1000):
                provider.read_batch(max_items=max_items)
                raise sqlite3.OperationalError("database-locked")

        wrapped = live_observation_module._ReplayableBatchProvider(Provider())
        with self.assertRaisesRegex(sqlite3.OperationalError, "database-locked") as raised:
            live_observation_module._poll_acknowledged(
                Engine(),
                wrapped,
                max_items=1,
            )

        self.assertFalse(wrapped.has_inflight)
        self.assertTrue(
            any(
                "provider pending-snapshot reset also failed: SystemExit: reset-stop"
                in note
                for note in getattr(raised.exception, "__notes__", ())
            )
        )

    def test_open_store_poll_does_not_rescan_append_only_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                mirror = MarketMirror.from_store(store)
                updates = BoundedMirrorInvalidationBuffer(mirror)
                health_store = SourceHealthStore(root / "source_health.json")

                with patch.object(
                    store,
                    "events",
                    side_effect=AssertionError("history rescan is forbidden"),
                ):
                    stats = poll_open_market_store_once(
                        store,
                        health_store,
                        self._provider(),
                        mirror_updates=updates,
                        max_items=10,
                        clock=lambda: _RECEIVE_TIME,
                    )

                self.assertEqual(stats.accepted, 2)
                self.assertEqual(len(mirror.snapshot()), 2)
                self.assertEqual(updates.pending_count, 2)
            finally:
                store.close()

    def test_long_lived_reconciliation_invalidates_preexisting_positive_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._observe(tmp)
            self.assertEqual(first.stats.accepted, 2)

            mirror = MarketMirror()
            updates = BoundedMirrorInvalidationBuffer(mirror)
            repeated = observe_workspace_once(
                tmp,
                self._provider(),
                max_items=10,
                clock=lambda: _RECEIVE_TIME,
                mirror_updates=updates,
            )

            self.assertEqual(repeated.stats.accepted, 0)
            active = mirror.active_view(
                as_of=datetime.fromisoformat(_RECEIVE_TIME),
                max_age=timedelta(minutes=2),
                source_ids="live-fixture",
            )
            self.assertEqual(len(active.events), 2)

            batch = updates.drain(max_items=10)
            self.assertFalse(batch.full_refresh_required)
            self.assertFalse(batch.has_more)
            self.assertEqual(
                set(batch.changed_keys),
                {
                    (event.source_id, event.quote_key)
                    for event in active.events
                },
            )

    def test_long_lived_reconciliation_promotes_same_sequence_positive_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self._observe(tmp)
            self.assertEqual(first.stats.accepted, 2)
            promoted = first.current_quotes[0]

            mirror = MarketMirror()
            mirror._apply_with_causal_authority(
                promoted,
                decision_causal=False,
            )
            self.assertEqual(mirror.causal_view().events, ())
            updates = BoundedMirrorInvalidationBuffer(mirror)

            repeated = observe_workspace_once(
                tmp,
                self._provider(),
                max_items=10,
                clock=lambda: _RECEIVE_TIME,
                mirror_updates=updates,
            )

            causal = mirror.causal_view(
                source_ids=promoted.source_id,
                selection_ids=promoted.selection_id,
            )
            self.assertEqual(
                tuple(event.dedupe_key for event in causal.events),
                (promoted.dedupe_key,),
            )
            invalidations = updates.drain(max_items=10)
            self.assertIn(
                (promoted.source_id, promoted.quote_key),
                invalidations.changed_keys,
            )
            self.assertEqual(repeated.stats.accepted, 0)

    def test_long_lived_reconciliation_preserves_generation_zero_as_noncausal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "market.db"
            legacy = MarketEvent(
                event_id="legacy-event",
                market_id="winner",
                selection_id="legacy-selection",
                decimal_odds=Decimal("2.20"),
                observed_ts="2026-09-12T20:00:00+00:00",
                ingest_ts="2026-09-12T20:00:00+00:00",
                source_id="live-fixture",
                sequence=3,
                status="open",
                source_ts="2026-09-12T19:59:59+00:00",
            )
            payload = storage_module._canonical_payload(legacy)

            raw = sqlite3.connect(path)
            try:
                raw.execute(
                    """CREATE TABLE market_events (
                        dedupe_key TEXT PRIMARY KEY,
                        quote_key TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        market_id TEXT NOT NULL,
                        selection_id TEXT NOT NULL,
                        decimal_odds TEXT NOT NULL,
                        observed_ts TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        payload_json TEXT NOT NULL
                    )"""
                )
                raw.execute(
                    """CREATE TABLE current_quotes (
                        source_id TEXT NOT NULL,
                        quote_key TEXT NOT NULL,
                        observed_ts TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (source_id, quote_key)
                    )"""
                )
                raw.execute(
                    """INSERT INTO market_events
                       (dedupe_key,quote_key,event_id,market_id,selection_id,
                        decimal_odds,observed_ts,source_id,sequence,payload_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        legacy.dedupe_key,
                        legacy.quote_key,
                        legacy.event_id,
                        legacy.market_id,
                        legacy.selection_id,
                        str(legacy.decimal_odds),
                        legacy.observed_ts,
                        legacy.source_id,
                        legacy.sequence,
                        payload,
                    ),
                )
                raw.execute(
                    """INSERT INTO current_quotes
                       (source_id,quote_key,observed_ts,sequence,payload_json)
                       VALUES (?,?,?,?,?)""",
                    (
                        legacy.source_id,
                        legacy.quote_key,
                        legacy.observed_ts,
                        legacy.sequence,
                        payload,
                    ),
                )
                raw.commit()
            finally:
                raw.close()

            mirror = MarketMirror()
            # Seed an intentionally wrong in-memory provenance claim for the exact
            # generation-zero value. Canonical reconciliation must revoke causal
            # authority even though provider sequence/payload are unchanged.
            mirror.apply(legacy)
            self.assertEqual(
                tuple(event.dedupe_key for event in mirror.causal_view().events),
                (legacy.dedupe_key,),
            )
            updates = BoundedMirrorInvalidationBuffer(mirror)
            result = observe_workspace_once(
                root,
                self._provider(),
                max_items=10,
                clock=lambda: _RECEIVE_TIME,
                mirror_updates=updates,
            )

            self.assertEqual(result.stats.accepted, 2)
            self.assertIn(
                legacy.dedupe_key,
                {
                    event.dedupe_key
                    for event in mirror.view(source_ids="live-fixture").events
                },
            )
            self.assertEqual(
                mirror.active_view(
                    as_of=datetime.fromisoformat(_RECEIVE_TIME),
                    max_age=timedelta(minutes=2),
                    event_ids="legacy-event",
                ).events,
                (),
            )
            active_live = mirror.active_view(
                as_of=datetime.fromisoformat(_RECEIVE_TIME),
                max_age=timedelta(minutes=2),
                source_ids="live-fixture",
            )
            self.assertEqual(len(active_live.events), 2)
            self.assertEqual(len(result.current_quotes), 2)
            self.assertNotIn(
                legacy.dedupe_key,
                {event.dedupe_key for event in result.current_quotes},
            )
            invalidations = updates.drain(max_items=10)
            self.assertFalse(invalidations.full_refresh_required)
            self.assertFalse(invalidations.has_more)
            self.assertIn(
                (legacy.source_id, legacy.quote_key),
                invalidations.changed_keys,
            )
            self.assertEqual(len(invalidations.changed_keys), 3)

    def test_worker_rejects_noncallable_task_before_claiming_slot(self):
        worker = OneShotObservationWorker()

        with self.assertRaisesRegex(TypeError, "task must be callable"):
            worker.start(None)

        self.assertFalse(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertIsNone(worker.poll())

    def test_worker_refuses_second_start_until_terminal_message_is_consumed(self):
        # Build the real observation result outside the worker timing window. This
        # test owns the worker single-flight/message-consumption contract; SQLite
        # startup latency is covered by the workspace-observer integration test and
        # must not become an accidental two-second completion SLA for live I/O.
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._observe(tmp)

        worker = OneShotObservationWorker()
        entered = threading.Event()
        release = threading.Event()

        def slow_task():
            entered.set()
            release.wait(timeout=2)
            return expected

        self.assertTrue(worker.start(slow_task))
        self.assertTrue(entered.wait(timeout=1))
        self.assertTrue(worker.busy)
        self.assertFalse(worker.start(slow_task))
        release.set()
        message = self._wait_for_message(worker)
        self.assertIs(message.result, expected)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)

    def test_worker_thread_is_non_daemon_while_durable_observation_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._observe(tmp)

        worker = OneShotObservationWorker()
        entered = threading.Event()
        release = threading.Event()

        def slow_task():
            entered.set()
            release.wait(timeout=2)
            return expected

        self.assertTrue(worker.start(slow_task))
        self.assertTrue(entered.wait(timeout=1))
        self.assertTrue(worker.busy)
        self.assertIsNotNone(worker._thread)
        self.assertFalse(worker._thread.daemon)
        release.set()
        message = self._wait_for_message(worker)
        self.assertIs(message.result, expected)
        self.assertFalse(worker.busy)

    def test_worker_thread_start_failure_publishes_terminal_error_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._observe(tmp)

        worker = OneShotObservationWorker()
        task_ran = threading.Event()

        def task():
            task_ran.set()
            return expected

        with patch.object(
            threading.Thread,
            "start",
            side_effect=RuntimeError("can't start new thread"),
        ):
            self.assertTrue(worker.start(task))

        self.assertFalse(task_ran.is_set())
        self.assertTrue(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertFalse(worker.start(task))
        failed = self._wait_for_message(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "RuntimeError: can't start new thread")
        self.assertFalse(worker.busy)

        self.assertTrue(worker.start(task))
        message = self._wait_for_message(worker)
        self.assertTrue(task_ran.is_set())
        self.assertIs(message.result, expected)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)

    def test_worker_thread_constructor_non_runtime_failure_is_terminal_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._observe(tmp)

        worker = OneShotObservationWorker()
        task_ran = threading.Event()

        def task():
            task_ran.set()
            return expected

        with patch(
            "autosport.live_observation.threading.Thread",
            side_effect=OSError("thread construction failed"),
        ):
            self.assertTrue(worker.start(task))

        self.assertFalse(task_ran.is_set())
        self.assertTrue(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertFalse(worker.start(task))
        failed = self._wait_for_message(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "OSError: thread construction failed")
        self.assertFalse(worker.busy)

        self.assertTrue(worker.start(task))
        message = self._wait_for_message(worker)
        self.assertTrue(task_ran.is_set())
        self.assertIs(message.result, expected)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)

    def test_worker_thread_start_non_runtime_failure_is_terminal_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._observe(tmp)

        worker = OneShotObservationWorker()
        task_ran = threading.Event()

        def task():
            task_ran.set()
            return expected

        with patch.object(
            threading.Thread,
            "start",
            side_effect=OSError("thread start failed"),
        ):
            self.assertTrue(worker.start(task))

        self.assertFalse(task_ran.is_set())
        self.assertTrue(worker.busy)
        self.assertIsNone(worker._thread)
        self.assertFalse(worker.start(task))
        failed = self._wait_for_message(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "OSError: thread start failed")
        self.assertFalse(worker.busy)

        self.assertTrue(worker.start(task))
        message = self._wait_for_message(worker)
        self.assertTrue(task_ran.is_set())
        self.assertIs(message.result, expected)
        self.assertIsNone(message.error)
        self.assertFalse(worker.busy)

    def test_worker_converts_exception_to_terminal_error_message(self):
        worker = OneShotObservationWorker()
        self.assertTrue(worker.start(lambda: (_ for _ in ()).throw(RuntimeError("network-test"))))
        message = self._wait_for_message(worker)
        self.assertIsNone(message.result)
        self.assertEqual(message.error, "RuntimeError: network-test")
        self.assertFalse(worker.busy)

    def test_worker_unprintable_exception_still_publishes_terminal_error(self):
        class UnprintableError(RuntimeError):
            def __str__(self) -> str:
                raise RuntimeError("stringification-failed")

        worker = OneShotObservationWorker()

        def task():
            raise UnprintableError()

        self.assertTrue(worker.start(task))
        failed = self._wait_for_message(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(
            failed.error,
            "UnprintableError: <unprintable exception>",
        )
        self.assertFalse(worker.busy)

    def test_worker_converts_system_exit_to_terminal_error_and_allows_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = self._observe(tmp)

        worker = OneShotObservationWorker()

        def exit_task():
            raise SystemExit("live-stop")

        self.assertTrue(worker.start(exit_task))
        failed = self._wait_for_message(worker)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "SystemExit: live-stop")
        self.assertFalse(worker.busy)

        self.assertTrue(worker.start(lambda: expected))
        completed = self._wait_for_message(worker)
        self.assertIs(completed.result, expected)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)

    def test_presentation_is_deterministic_text_for_screen_reader_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._observe(tmp)
            summary = observation_summary(result)
            lines = observation_quote_lines(result)
            self.assertIn("стан=healthy", summary)
            self.assertIn("поточних=2", summary)
            self.assertEqual(len(lines), 2)
            self.assertIn("player-a", lines[0])
            self.assertIn("коефіцієнт 1.80", lines[0])
            self.assertIn("час джерела 2026-09-12T19:59:59+00:00", lines[0])


if __name__ == "__main__":
    unittest.main()
