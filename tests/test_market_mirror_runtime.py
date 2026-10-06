import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.domain import MarketEvent
from autosport.market_bus import MarketEventBus
from autosport.market_mirror import MarketMirror, MirrorUpdate
from autosport.market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
    FocusedMirrorRegistryChanged,
)
from autosport.storage import SQLiteMarketStore


class BoundedMirrorInvalidationBufferTests(unittest.TestCase):
    @staticmethod
    def event(
        *,
        selection: str = "selection-1",
        sequence: int = 1,
        odds: str = "2.00",
        source_id: str = "provider-a",
        event_id: str = "event-1",
        market_id: str = "market-1",
    ) -> MarketEvent:
        timestamp = f"2026-09-16T19:00:{sequence:02d}+00:00"
        return MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection,
            decimal_odds=Decimal(odds),
            observed_ts=timestamp,
            source_id=source_id,
            sequence=sequence,
            status="open",
            source_ts=timestamp,
            ingest_ts=timestamp,
        )

    def test_market_bus_persists_before_mirror_subscriber_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                mirror = MarketMirror()
                runtime = BoundedMirrorInvalidationBuffer(mirror)
                bus = MarketEventBus(store)
                observed_durable_sequences: list[int] = []

                def subscriber(event: MarketEvent) -> None:
                    durable = store.events()
                    observed_durable_sequences.append(durable[-1].sequence)
                    runtime.accept_persisted(event)

                bus.subscribe(subscriber)
                self.assertTrue(bus.publish(self.event(sequence=1)))

                self.assertEqual(observed_durable_sequences, [1])
                self.assertEqual(store.events()[-1].sequence, 1)
                self.assertEqual(
                    mirror.get("provider-a", "event-1", "market-1", "selection-1").sequence,
                    1,
                )
                batch = runtime.drain(max_items=1)
                self.assertEqual(
                    batch.changed_keys,
                    (("provider-a", "event-1|market-1|selection-1"),),
                )
                self.assertFalse(batch.full_refresh_required)
                self.assertFalse(batch.has_more)
            finally:
                store.close()

    def test_repeated_material_updates_coalesce_one_affected_quote(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=2)

        first = runtime.accept_persisted(self.event(sequence=1, odds="2.00"))
        second = runtime.accept_persisted(self.event(sequence=2, odds="2.10"))

        self.assertEqual(first.status, MirrorUpdate.APPLIED)
        self.assertEqual(second.status, MirrorUpdate.APPLIED)
        self.assertEqual(runtime.pending_count, 1)
        self.assertEqual(
            mirror.get("provider-a", "event-1", "market-1", "selection-1").decimal_odds,
            Decimal("2.10"),
        )
        self.assertEqual(
            runtime.drain(max_items=2).changed_keys,
            (("provider-a", "event-1|market-1|selection-1"),),
        )

    def test_distinct_key_overflow_promotes_to_full_refresh_without_losing_mirror_truth(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=1)

        runtime.accept_persisted(self.event(selection="selection-a", sequence=1))
        runtime.accept_persisted(self.event(selection="selection-b", sequence=1))

        self.assertTrue(runtime.full_refresh_required)
        self.assertEqual(runtime.pending_count, 0)
        self.assertEqual(
            tuple(event.selection_id for event in mirror.snapshot()),
            ("selection-a", "selection-b"),
        )

        overflow = runtime.drain(max_items=1)
        self.assertTrue(overflow.full_refresh_required)
        self.assertEqual(overflow.changed_keys, ())
        self.assertFalse(overflow.has_more)
        self.assertFalse(runtime.full_refresh_required)

        runtime.accept_persisted(self.event(selection="selection-c", sequence=1))
        recovered = runtime.drain(max_items=1)
        self.assertFalse(recovered.full_refresh_required)
        self.assertEqual(
            recovered.changed_keys,
            (("provider-a", "event-1|market-1|selection-c"),),
        )

    def test_bounded_drain_preserves_first_dirty_order_and_reports_more(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=4)
        for selection in ("a", "b", "c"):
            runtime.accept_persisted(self.event(selection=selection, sequence=1))

        first = runtime.drain(max_items=2)
        second = runtime.drain(max_items=2)

        self.assertEqual(
            first.changed_keys,
            (
                ("provider-a", "event-1|market-1|a"),
                ("provider-a", "event-1|market-1|b"),
            ),
        )
        self.assertTrue(first.has_more)
        self.assertEqual(
            second.changed_keys,
            (("provider-a", "event-1|market-1|c"),),
        )
        self.assertFalse(second.has_more)

    def test_duplicate_and_stale_events_do_not_create_downstream_work(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        current = self.event(sequence=3, odds="2.30")

        runtime.accept_persisted(current)
        runtime.drain()
        duplicate = runtime.accept_persisted(
            MarketEvent.from_dict(
                {
                    **current.to_dict(),
                    "observed_ts": "2026-09-16T19:01:00+00:00",
                    "ingest_ts": "2026-09-16T19:01:01+00:00",
                }
            )
        )
        stale = runtime.accept_persisted(self.event(sequence=2, odds="2.10"))

        self.assertEqual(duplicate.status, MirrorUpdate.DUPLICATE)
        self.assertEqual(stale.status, MirrorUpdate.STALE)
        self.assertEqual(runtime.pending_count, 0)
        self.assertEqual(runtime.drain().changed_keys, ())

    def test_reconcile_trusted_store_routes_missed_durable_update_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                event = self.event(sequence=1, odds="2.10")
                self.assertEqual(
                    MarketEventBus(store)._publish_many_live_ingestion([event]),
                    1,
                )
                mirror = MarketMirror()
                runtime = BoundedMirrorInvalidationBuffer(mirror)

                first = runtime.reconcile_trusted_store(store)

                self.assertEqual(len(first), 1)
                self.assertEqual(first[0].status, MirrorUpdate.APPLIED)
                self.assertEqual(
                    mirror.get(
                        "provider-a",
                        "event-1",
                        "market-1",
                        "selection-1",
                    ).decimal_odds,
                    Decimal("2.10"),
                )
                self.assertEqual(
                    runtime.drain().changed_keys,
                    (("provider-a", "event-1|market-1|selection-1"),),
                )

                second = runtime.reconcile_trusted_store(store)
                self.assertEqual(len(second), 1)
                self.assertEqual(second[0].status, MirrorUpdate.DUPLICATE)
                self.assertEqual(runtime.pending_count, 0)
            finally:
                store.close()

    def test_reconcile_holds_mirror_cut_against_direct_apply_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(sequence=1, odds="2.00")
                second = self.event(sequence=2, odds="2.20")
                self.assertEqual(
                    MarketEventBus(store)._publish_many_live_ingestion([first]),
                    1,
                )
                mirror = MarketMirror.from_live_store(store)
                runtime = BoundedMirrorInvalidationBuffer(mirror)

                entered_receipt_query = threading.Event()
                release_receipt_query = threading.Event()
                paused = False

                def trace(statement: str) -> None:
                    nonlocal paused
                    normalized = " ".join(statement.split()).lower()
                    if (
                        not paused
                        and "from market_events as m" in normalized
                        and "where m.dedupe_key=" in normalized
                    ):
                        paused = True
                        entered_receipt_query.set()
                        if not release_receipt_query.wait(timeout=2):
                            raise AssertionError("test did not release receipt query")

                store.connection.set_trace_callback(trace)
                reconcile_result: list[tuple] = []
                reconcile_error: list[BaseException] = []

                def reconcile() -> None:
                    try:
                        reconcile_result.append(runtime.reconcile_trusted_store(store))
                    except BaseException as exc:
                        reconcile_error.append(exc)

                reconcile_thread = threading.Thread(target=reconcile)
                reconcile_thread.start()
                self.assertTrue(entered_receipt_query.wait(timeout=2))

                direct_done = threading.Event()
                direct_result = []

                def direct_apply() -> None:
                    direct_result.append(mirror.apply(second))
                    direct_done.set()

                direct_thread = threading.Thread(target=direct_apply)
                direct_thread.start()
                self.assertFalse(direct_done.wait(timeout=0.05))

                release_receipt_query.set()
                reconcile_thread.join(timeout=2)
                direct_thread.join(timeout=2)
                self.assertFalse(reconcile_thread.is_alive())
                self.assertFalse(direct_thread.is_alive())
                self.assertEqual(reconcile_error, [])
                self.assertEqual(
                    tuple(result.status for result in reconcile_result[0]),
                    (MirrorUpdate.DUPLICATE,),
                )
                self.assertEqual(direct_result[0].status, MirrorUpdate.APPLIED)
                self.assertEqual(mirror.snapshot()[0].sequence, 2)
            finally:
                store.connection.set_trace_callback(None)
                store.close()

    def test_reconcile_trusted_store_overflow_requires_full_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                events = [
                    self.event(selection="selection-a", sequence=1, odds="2.10"),
                    self.event(selection="selection-b", sequence=1, odds="2.20"),
                ]
                self.assertEqual(
                    MarketEventBus(store)._publish_many_live_ingestion(events),
                    2,
                )
                mirror = MarketMirror()
                runtime = BoundedMirrorInvalidationBuffer(
                    mirror,
                    max_dirty_keys=1,
                )

                results = runtime.reconcile_trusted_store(store)

                self.assertEqual(
                    tuple(result.status for result in results),
                    (MirrorUpdate.APPLIED, MirrorUpdate.APPLIED),
                )
                self.assertEqual(len(mirror.snapshot()), 2)
                self.assertTrue(runtime.full_refresh_required)
                self.assertEqual(runtime.pending_count, 0)
                batch = runtime.drain()
                self.assertTrue(batch.full_refresh_required)
                self.assertEqual(batch.changed_keys, ())
                self.assertFalse(batch.has_more)
            finally:
                store.close()

    def test_first_reconcile_rejects_preloaded_mirror_from_other_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            first_store = SQLiteMarketStore(Path(first_directory) / "market.db")
            second_store = SQLiteMarketStore(Path(second_directory) / "market.db")
            try:
                event = self.event(selection="selection-a", sequence=1)
                self.assertEqual(
                    MarketEventBus(first_store)._publish_many_live_ingestion([event]),
                    1,
                )
                mirror = MarketMirror.from_live_store(first_store)
                runtime = BoundedMirrorInvalidationBuffer(mirror)
                before = mirror.snapshot()

                with self.assertRaisesRegex(
                    ValueError,
                    "pre-existing mirror state is not trusted",
                ):
                    runtime.reconcile_trusted_store(second_store)

                self.assertEqual(mirror.snapshot(), before)
                self.assertEqual(runtime.pending_count, 0)
                self.assertFalse(runtime.full_refresh_required)
            finally:
                first_store.close()
                second_store.close()

    def test_first_reconcile_accepts_older_receipt_trusted_state_from_same_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                first = self.event(sequence=1, odds="2.00")
                second = self.event(sequence=2, odds="2.20")
                self.assertEqual(
                    MarketEventBus(store)._publish_many_live_ingestion([first, second]),
                    2,
                )
                mirror = MarketMirror()
                self.assertEqual(mirror.apply(first).status, MirrorUpdate.APPLIED)
                runtime = BoundedMirrorInvalidationBuffer(mirror)

                results = runtime.reconcile_trusted_store(store)

                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].status, MirrorUpdate.APPLIED)
                self.assertEqual(
                    mirror.get(
                        "provider-a",
                        "event-1",
                        "market-1",
                        "selection-1",
                    ).sequence,
                    2,
                )
                self.assertEqual(
                    runtime.drain().changed_keys,
                    (("provider-a", "event-1|market-1|selection-1"),),
                )
            finally:
                store.close()

    def test_reconcile_same_path_authority_rollback_rejects_retained_mirror_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            event = self.event(selection="selection-a", sequence=1)
            self.assertEqual(
                MarketEventBus(store)._publish_many_live_ingestion([event]),
                1,
            )
            mirror = MarketMirror.from_live_store(store)
            runtime = BoundedMirrorInvalidationBuffer(mirror)
            runtime.reconcile_trusted_store(store)
            runtime.drain()
            before = mirror.snapshot()
            store.close()

            connection = SQLiteMarketStore(path)
            try:
                connection.connection.execute("DELETE FROM trusted_live_current_quotes")
                connection.connection.execute("DELETE FROM market_event_live_receipts")
                connection.connection.execute("DELETE FROM current_quotes")
                connection.connection.execute("DELETE FROM market_events")
                connection.connection.commit()
            finally:
                connection.close()

            reopened = SQLiteMarketStore(path)
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "pre-existing mirror state is not trusted",
                ):
                    runtime.reconcile_trusted_store(reopened)

                self.assertEqual(mirror.snapshot(), before)
                self.assertEqual(runtime.pending_count, 0)
                self.assertFalse(runtime.full_refresh_required)
            finally:
                reopened.close()

    def test_reconcile_trusted_store_rejects_cross_workspace_reuse_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            first_store = SQLiteMarketStore(Path(first_directory) / "market.db")
            second_store = SQLiteMarketStore(Path(second_directory) / "market.db")
            try:
                first_event = self.event(selection="selection-a", sequence=1)
                second_event = self.event(
                    selection="selection-b",
                    sequence=1,
                    odds="3.00",
                )
                self.assertEqual(
                    MarketEventBus(first_store)._publish_many_live_ingestion([first_event]),
                    1,
                )
                self.assertEqual(
                    MarketEventBus(second_store)._publish_many_live_ingestion([second_event]),
                    1,
                )
                mirror = MarketMirror()
                runtime = BoundedMirrorInvalidationBuffer(mirror)
                runtime.reconcile_trusted_store(first_store)
                runtime.drain()
                before = mirror.snapshot()

                with self.assertRaisesRegex(
                    ValueError,
                    "different market store",
                ):
                    runtime.reconcile_trusted_store(second_store)

                self.assertEqual(mirror.snapshot(), before)
                self.assertEqual(runtime.pending_count, 0)
            finally:
                first_store.close()
                second_store.close()

    def test_reconcile_rejects_rebound_store_path_before_mirror_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            first_store = SQLiteMarketStore(Path(first_directory) / "market.db")
            second_store = SQLiteMarketStore(Path(second_directory) / "market.db")
            try:
                first_event = self.event(selection="selection-a", sequence=1)
                second_event = self.event(
                    selection="selection-b",
                    sequence=1,
                    odds="3.00",
                )
                self.assertEqual(
                    MarketEventBus(first_store)._publish_many_live_ingestion([first_event]),
                    1,
                )
                self.assertEqual(
                    MarketEventBus(second_store)._publish_many_live_ingestion([second_event]),
                    1,
                )
                mirror = MarketMirror()
                runtime = BoundedMirrorInvalidationBuffer(mirror)
                runtime.reconcile_trusted_store(first_store)
                runtime.drain()
                before = mirror.snapshot()

                second_store.path = first_store.path
                with self.assertRaisesRegex(
                    ValueError,
                    "path does not match the opened SQLite database",
                ):
                    runtime.reconcile_trusted_store(second_store)

                self.assertEqual(mirror.snapshot(), before)
                self.assertEqual(runtime.pending_count, 0)
                self.assertFalse(runtime.full_refresh_required)
            finally:
                first_store.close()
                second_store.close()

    def test_reconcile_trusted_store_requires_exact_store_and_hides_dependency_hooks(self) -> None:
        class StoreSubclass(SQLiteMarketStore):
            pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                runtime = BoundedMirrorInvalidationBuffer(MarketMirror())
                with self.assertRaises(TypeError):
                    runtime.reconcile_trusted_store(
                        store,
                        _trusted_current=lambda _store: {},
                    )

                with patch.object(
                    SQLiteMarketStore,
                    "trusted_live_current_by_source",
                    side_effect=AssertionError(
                        "runtime rebinding must not replace sealed trusted reader"
                    ),
                ):
                    self.assertEqual(runtime.reconcile_trusted_store(store), ())

                store.close()
                subclass = StoreSubclass(path)
                try:
                    with self.assertRaisesRegex(
                        TypeError,
                        "exact SQLiteMarketStore",
                    ):
                        runtime.reconcile_trusted_store(subclass)
                finally:
                    subclass.close()
            finally:
                try:
                    store.close()
                except Exception:
                    pass

    def test_focused_dependencies_route_only_affected_provider_and_selection(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register(
            "decision-provider-a",
            source_ids="provider-a",
            event_ids="event-1",
            market_ids="market-1",
            selection_ids="selection-a",
        )
        dependencies.register(
            "decision-provider-b",
            source_ids="provider-b",
            event_ids="event-1",
            market_ids="market-1",
            selection_ids="selection-a",
        )
        dependencies.register(
            "decision-other-selection",
            source_ids="provider-a",
            event_ids="event-1",
            market_ids="market-1",
            selection_ids="selection-b",
        )

        runtime.accept_persisted(
            self.event(source_id="provider-a", selection="selection-a", sequence=1)
        )
        self.assertEqual(
            dependencies.affected_inputs(runtime.drain()),
            ("decision-provider-a",),
        )

        runtime.accept_persisted(
            self.event(source_id="provider-b", selection="selection-a", sequence=1)
        )
        self.assertEqual(
            dependencies.affected_inputs(runtime.drain()),
            ("decision-provider-b",),
        )

        runtime.accept_persisted(
            self.event(source_id="provider-a", selection="selection-c", sequence=1)
        )
        self.assertEqual(dependencies.affected_inputs(runtime.drain()), ())

        view = dependencies.decision_view(
            "decision-provider-a",
            as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(len(view.events), 1)
        self.assertEqual(view.events[0].source_id, "provider-a")
        self.assertEqual(view.events[0].selection_id, "selection-a")

    def test_focused_dependencies_ignore_stale_and_duplicate_updates(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register("decision", source_ids="provider-a", selection_ids="selection-1")
        current = self.event(sequence=3, odds="2.30")

        runtime.accept_persisted(current)
        self.assertEqual(dependencies.affected_inputs(runtime.drain()), ("decision",))

        duplicate = MarketEvent.from_dict(
            {
                **current.to_dict(),
                "observed_ts": "2026-09-16T19:01:00+00:00",
                "ingest_ts": "2026-09-16T19:01:01+00:00",
            }
        )
        runtime.accept_persisted(duplicate)
        runtime.accept_persisted(self.event(sequence=2, odds="2.10"))

        self.assertEqual(dependencies.affected_inputs(runtime.drain()), ())

    def test_focused_incremental_reads_do_not_snapshot_unrelated_mirror(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        runtime.accept_persisted(
            self.event(source_id="provider-a", selection="selection-a", sequence=1)
        )
        runtime.accept_persisted(
            self.event(source_id="provider-a", selection="selection-z", sequence=1)
        )
        runtime.drain()

        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register(
            "decision-a",
            source_ids="provider-a",
            selection_ids="selection-a",
        )

        runtime.accept_persisted(
            self.event(
                source_id="provider-a",
                selection="selection-a",
                sequence=2,
                odds="2.10",
            )
        )
        with patch.object(
            mirror,
            "snapshot",
            side_effect=AssertionError("whole mirror snapshot is forbidden"),
        ):
            affected = dependencies.affected_inputs(runtime.drain())
            view = dependencies.decision_view(
                "decision-a",
                as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
                max_age=timedelta(minutes=1),
            )

        self.assertEqual(affected, ("decision-a",))
        self.assertEqual(len(view.events), 1)
        self.assertEqual(view.events[0].selection_id, "selection-a")
        self.assertEqual(view.events[0].sequence, 2)
        self.assertNotIn(
            ("provider-a", "event-1|market-1|selection-z"),
            dependencies.all_matching_keys(),
        )

    def test_dependency_registration_retries_if_mirror_advances_after_snapshot(self) -> None:
        mirror = MarketMirror()
        dependencies = FocusedMirrorDependencyIndex(mirror)
        event = self.event(selection="selection-a", sequence=1, odds="2.10")
        real_view = mirror.view
        injected = {"done": False}

        def capture_then_advance(*args, **kwargs):
            captured = real_view(*args, **kwargs)
            if not injected["done"]:
                injected["done"] = True
                mirror.apply(event)
            return captured

        with patch.object(mirror, "view", side_effect=capture_then_advance):
            dependency = dependencies.register(
                "decision-a",
                selection_ids="selection-a",
            )

        self.assertTrue(injected["done"])
        self.assertEqual(dependency.input_id, "decision-a")
        self.assertEqual(
            dependencies.matching_keys("decision-a"),
            ((event.source_id, event.quote_key),),
        )

    def test_coherent_incremental_views_share_one_exact_mirror_revision(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register("decision-a", selection_ids="selection-a")
        dependencies.register("decision-b", selection_ids="selection-b")

        runtime.accept_persisted(
            self.event(selection="selection-a", sequence=1, odds="2.00")
        )
        runtime.accept_persisted(
            self.event(selection="selection-b", sequence=1, odds="3.00")
        )
        runtime.accept_persisted(
            self.event(selection="unrelated", sequence=1, odds="9.00")
        )
        affected = dependencies.affected_inputs(runtime.drain())
        self.assertEqual(affected, ("decision-a", "decision-b"))

        with (
            patch.object(
                mirror,
                "snapshot",
                side_effect=AssertionError("whole mirror snapshot is forbidden"),
            ),
            patch.object(
                mirror,
                "view",
                side_effect=AssertionError("whole mirror view is forbidden"),
            ),
        ):
            views = dependencies.coherent_decision_views(
                ("decision-a", "decision-b"),
                as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
                max_age=timedelta(minutes=1),
                incremental=True,
            )

        self.assertEqual(set(views), {"decision-a", "decision-b"})
        self.assertEqual(
            {snapshot.revision for snapshot in views.values()},
            {mirror.revision},
        )
        self.assertEqual(
            tuple(event.selection_id for event in views["decision-a"].events),
            ("selection-a",),
        )
        self.assertEqual(
            tuple(event.selection_id for event in views["decision-b"].events),
            ("selection-b",),
        )
        self.assertNotIn(
            "unrelated",
            {
                event.selection_id
                for snapshot in views.values()
                for event in snapshot.events
            },
        )

    def test_coherent_views_do_not_share_mutable_event_metadata(self) -> None:
        mirror = MarketMirror()
        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register("decision-a", selection_ids="selection-a")
        dependencies.register("decision-b", selection_ids="selection-a")
        base = self.event(selection="selection-a", sequence=1)
        event = MarketEvent.from_dict(
            {
                **base.to_dict(),
                "metadata": {"nested": {"origin": "canonical"}},
            }
        )
        mirror.apply(event)

        views = dependencies.coherent_decision_views(
            ("decision-a", "decision-b"),
            as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
            max_age=timedelta(minutes=1),
            incremental=False,
        )
        views["decision-a"].events[0].metadata["nested"]["origin"] = "mutated-a"

        self.assertEqual(
            views["decision-b"].events[0].metadata,
            {"nested": {"origin": "canonical"}},
        )
        self.assertEqual(
            mirror.snapshot()[0].metadata,
            {"nested": {"origin": "canonical"}},
        )

    def test_atomic_drain_and_route_retains_dirty_state_on_routing_failure(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register("decision-all", source_ids="provider-a")
        event = self.event(selection="selection-a", sequence=1, odds="2.00")
        runtime.accept_persisted(event)

        with patch.object(
            dependencies,
            "affected_inputs",
            side_effect=RuntimeError("simulated routing failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated routing failure"):
                runtime.drain_and_route(dependencies)

        self.assertEqual(runtime.pending_count, 1)
        batch, affected = runtime.drain_and_route(dependencies)

        self.assertEqual(
            batch.changed_keys,
            ((event.source_id, event.quote_key),),
        )
        self.assertEqual(affected, ("decision-all",))
        self.assertEqual(runtime.pending_count, 0)
        self.assertEqual(
            dependencies.matching_keys("decision-all"),
            ((event.source_id, event.quote_key),),
        )


    def test_drained_unrouted_key_invalidates_captured_routing_generation(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register("decision-all", source_ids="provider-a")

        baseline = self.event(selection="selection-a", sequence=1, odds="2.00")
        runtime.accept_persisted(baseline)
        dependencies.affected_inputs(runtime.drain())
        captured_routing_revision = dependencies.routing_revision

        concurrent = self.event(selection="selection-b", sequence=2, odds="3.00")
        runtime.accept_persisted(concurrent)
        drained_but_unrouted = runtime.drain()

        # Mirror truth already includes the new key and the invalidation buffer is
        # empty, but incremental routing still reflects the older key set.
        self.assertEqual(runtime.pending_count, 0)
        self.assertEqual(dependencies.routing_revision, captured_routing_revision)
        torn = dependencies.coherent_decision_views(
            ("decision-all",),
            as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
            max_age=timedelta(minutes=1),
            incremental=True,
        )
        self.assertEqual(
            tuple(event.selection_id for event in torn["decision-all"].events),
            ("selection-a",),
        )
        self.assertEqual(torn["decision-all"].revision, mirror.revision)

        affected = dependencies.affected_inputs(drained_but_unrouted)
        self.assertEqual(affected, ("decision-all",))
        self.assertGreater(
            dependencies.routing_revision,
            captured_routing_revision,
        )
        with self.assertRaisesRegex(
            FocusedMirrorRegistryChanged,
            "routing changed before decision publication",
        ):
            with dependencies.hold_input_ids(
                ("decision-all",),
                expected_routing_revision=captured_routing_revision,
            ):
                self.fail("stale routing generation must not reach publication")


    def test_focused_dependency_overflow_fails_safe_to_all_registered_inputs(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=1)
        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register("selection-a", selection_ids="selection-a")
        dependencies.register("selection-b", selection_ids="selection-b")
        dependencies.register("unrelated", event_ids="event-2")

        runtime.accept_persisted(self.event(selection="selection-a", sequence=1))
        runtime.accept_persisted(self.event(selection="selection-b", sequence=1))
        batch = runtime.drain()

        self.assertTrue(batch.full_refresh_required)
        self.assertEqual(
            dependencies.affected_inputs(batch),
            ("selection-a", "selection-b", "unrelated"),
        )

    def test_focused_replay_uses_same_selectors_without_future_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                mirror = MarketMirror()
                runtime = BoundedMirrorInvalidationBuffer(mirror)
                dependencies = FocusedMirrorDependencyIndex(mirror)
                dependencies.register(
                    "decision",
                    source_ids="provider-a",
                    event_ids="event-1",
                    market_ids="market-1",
                    selection_ids="selection-1",
                )

                first = self.event(sequence=1, odds="2.00")
                future = self.event(sequence=2, odds="2.40")
                other_provider = self.event(
                    source_id="provider-b",
                    sequence=1,
                    odds="3.00",
                )
                for event in (first, future, other_provider):
                    store.append(event)
                    runtime.accept_persisted(event)
                runtime.drain()

                current = dependencies.decision_view(
                    "decision",
                    as_of=datetime(2026, 9, 16, 19, 0, 3, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=1),
                )
                replay = dependencies.replay_view(
                    "decision",
                    store,
                    as_of=datetime(
                        2026,
                        9,
                        16,
                        19,
                        0,
                        1,
                        500000,
                        tzinfo=timezone.utc,
                    ),
                    max_age=timedelta(minutes=1),
                )

                self.assertEqual(len(current.events), 1)
                self.assertEqual(current.events[0].sequence, 2)
                self.assertEqual(current.events[0].decimal_odds, Decimal("2.40"))
                self.assertEqual(len(replay.events), 1)
                self.assertEqual(replay.events[0].sequence, 1)
                self.assertEqual(replay.events[0].decimal_odds, Decimal("2.00"))
                self.assertEqual(replay.events[0].source_id, "provider-a")
            finally:
                store.close()

    def test_focused_dependency_registration_is_explicit_and_non_overwriting(self) -> None:
        mirror = MarketMirror()
        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register("decision", source_ids="provider-a")

        with self.assertRaises(ValueError):
            dependencies.register("decision", source_ids="provider-b")
        with self.assertRaises(ValueError):
            dependencies.register(" untrimmed ")
        with self.assertRaises(KeyError):
            dependencies.decision_view(
                "missing",
                as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
                max_age=timedelta(minutes=1),
            )

        self.assertTrue(dependencies.unregister("decision"))
        self.assertFalse(dependencies.unregister("decision"))

    def test_bounds_reject_boolean_and_nonpositive_values(self) -> None:
        mirror = MarketMirror()
        for value in (True, 0, -1):
            with self.subTest(max_dirty_keys=value):
                with self.assertRaises(ValueError):
                    BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=value)

        runtime = BoundedMirrorInvalidationBuffer(mirror)
        for value in (True, 0, -1):
            with self.subTest(max_items=value):
                with self.assertRaises(ValueError):
                    runtime.drain(max_items=value)


if __name__ == "__main__":
    unittest.main()
