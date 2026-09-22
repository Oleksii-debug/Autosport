import tempfile
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

    def test_incremental_batch_views_remain_one_revision_when_market_moves_after_capture(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        runtime.accept_persisted(
            self.event(source_id="provider-a", selection="selection-a", sequence=1)
        )
        runtime.accept_persisted(
            self.event(source_id="provider-a", selection="selection-b", sequence=1)
        )
        runtime.drain()

        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register(
            "decision-a",
            source_ids="provider-a",
            selection_ids="selection-a",
        )
        dependencies.register(
            "decision-b",
            source_ids="provider-a",
            selection_ids="selection-b",
        )

        real_read = mirror.active_view_for_keys

        def move_market_after_capture(keys, *, as_of, max_age):
            captured = real_read(keys, as_of=as_of, max_age=max_age)
            runtime.accept_persisted(
                self.event(
                    source_id="provider-a",
                    selection="selection-a",
                    sequence=2,
                    odds="2.10",
                )
            )
            runtime.accept_persisted(
                self.event(
                    source_id="provider-a",
                    selection="selection-b",
                    sequence=2,
                    odds="2.20",
                )
            )
            return captured

        with patch.object(
            mirror,
            "active_view_for_keys",
            side_effect=move_market_after_capture,
        ) as read:
            views = dependencies.incremental_decision_views(
                ("decision-a", "decision-b"),
                as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
                max_age=timedelta(minutes=1),
            )

        self.assertEqual(read.call_count, 1)
        self.assertEqual(views["decision-a"].revision, views["decision-b"].revision)
        self.assertEqual(
            [event.sequence for event in views["decision-a"].events],
            [1],
        )
        self.assertEqual(
            [event.sequence for event in views["decision-b"].events],
            [1],
        )
        self.assertEqual(
            mirror.get(
                "provider-a",
                "event-1",
                "market-1",
                "selection-a",
            ).sequence,
            2,
        )
        self.assertEqual(
            mirror.get(
                "provider-a",
                "event-1",
                "market-1",
                "selection-b",
            ).sequence,
            2,
        )

    def test_incremental_batch_views_preserve_routed_subset_without_full_snapshot(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        runtime.accept_persisted(
            self.event(source_id="provider-a", selection="selection-a", sequence=1)
        )
        runtime.accept_persisted(
            self.event(source_id="provider-a", selection="selection-b", sequence=1)
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
        dependencies.register(
            "decision-b",
            source_ids="provider-a",
            selection_ids="selection-b",
        )

        with patch.object(
            mirror,
            "snapshot",
            side_effect=AssertionError("whole mirror snapshot is forbidden"),
        ), patch.object(
            mirror,
            "active_view",
            side_effect=AssertionError("full active view is forbidden"),
        ):
            views = dependencies.incremental_decision_views(
                ("decision-a", "decision-b"),
                as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
                max_age=timedelta(minutes=1),
            )

        self.assertEqual(
            [event.selection_id for event in views["decision-a"].events],
            ["selection-a"],
        )
        self.assertEqual(
            [event.selection_id for event in views["decision-b"].events],
            ["selection-b"],
        )
        self.assertEqual(views["decision-a"].revision, views["decision-b"].revision)

    def test_focused_routed_key_cache_saturation_falls_back_without_partial_truth(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        dependencies = FocusedMirrorDependencyIndex(
            mirror,
            max_cached_keys_per_input=2,
        )
        dependencies.register("decision", source_ids="provider-a")

        for selection in ("selection-a", "selection-b", "selection-c"):
            runtime.accept_persisted(
                self.event(
                    source_id="provider-a",
                    selection=selection,
                    sequence=1,
                )
            )
            dependencies.affected_inputs(runtime.drain())

        self.assertEqual(dependencies.max_cached_keys_per_input, 2)
        self.assertFalse(dependencies.routed_cache_complete("decision"))
        with self.assertRaisesRegex(RuntimeError, "routed key cache is incomplete"):
            dependencies.matching_keys("decision")
        with self.assertRaisesRegex(RuntimeError, "routed key cache is incomplete"):
            dependencies.all_matching_keys()

        canonical = dependencies.decision_view(
            "decision",
            as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
            max_age=timedelta(minutes=1),
        )
        with patch.object(
            mirror,
            "active_view_for_keys",
            side_effect=AssertionError(
                "saturated routed cache must not use an incomplete key fast path"
            ),
        ):
            incremental = dependencies.incremental_decision_view(
                "decision",
                as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
                max_age=timedelta(minutes=1),
            )

        self.assertEqual(incremental, canonical)
        self.assertEqual(
            [event.selection_id for event in incremental.events],
            ["selection-a", "selection-b", "selection-c"],
        )

    def test_registration_with_more_matching_keys_than_budget_starts_in_fallback_mode(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        for selection in ("selection-a", "selection-b", "selection-c"):
            runtime.accept_persisted(
                self.event(
                    source_id="provider-a",
                    selection=selection,
                    sequence=1,
                )
            )

        dependencies = FocusedMirrorDependencyIndex(
            mirror,
            max_cached_keys_per_input=2,
        )
        dependencies.register("decision", source_ids="provider-a")

        self.assertFalse(dependencies.routed_cache_complete("decision"))
        with self.assertRaisesRegex(RuntimeError, "routed key cache is incomplete"):
            dependencies.matching_keys("decision")
        with patch.object(
            mirror,
            "active_view_for_keys",
            side_effect=AssertionError(
                "registration saturation must use canonical selector fallback"
            ),
        ):
            view = dependencies.incremental_decision_view(
                "decision",
                as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
                max_age=timedelta(minutes=1),
            )
        self.assertEqual(len(view.events), 3)

    def test_revision_resync_that_exceeds_key_budget_switches_to_canonical_fallback(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        for selection in ("selection-a", "selection-b"):
            runtime.accept_persisted(
                self.event(
                    source_id="provider-a",
                    selection=selection,
                    sequence=1,
                )
            )
        runtime.drain()

        dependencies = FocusedMirrorDependencyIndex(
            mirror,
            max_cached_keys_per_input=2,
        )
        dependencies.register("decision", source_ids="provider-a")
        self.assertTrue(dependencies.routed_cache_complete("decision"))

        # Advance canonical truth without routing its invalidation through this index.
        # The revision mismatch must resync, discover saturation, and fail over to the
        # selector path rather than publish an older two-key subset as revision 3.
        mirror.apply(
            self.event(
                source_id="provider-a",
                selection="selection-c",
                sequence=1,
            )
        )
        view = dependencies.incremental_decision_view(
            "decision",
            as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
            max_age=timedelta(minutes=1),
        )

        self.assertFalse(dependencies.routed_cache_complete("decision"))
        self.assertEqual(view.revision, 3)
        self.assertEqual(
            [event.selection_id for event in view.events],
            ["selection-a", "selection-b", "selection-c"],
        )

    def test_focused_routed_key_cache_budget_rejects_invalid_values(self) -> None:
        mirror = MarketMirror()
        for value in (0, -1, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "positive non-boolean integer",
                ):
                    FocusedMirrorDependencyIndex(
                        mirror,
                        max_cached_keys_per_input=value,
                    )

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

    def test_registration_refreshes_if_mirror_advances_before_publication(self) -> None:
        mirror = MarketMirror()
        runtime = BoundedMirrorInvalidationBuffer(mirror)
        runtime.accept_persisted(
            self.event(source_id="provider-a", selection="selection-a", sequence=1)
        )
        dependencies = FocusedMirrorDependencyIndex(mirror)

        real_view = mirror.view
        injected = False

        def advance_after_initial_capture(*args, **kwargs):
            nonlocal injected
            captured = real_view(*args, **kwargs)
            if not injected:
                injected = True
                runtime.accept_persisted(
                    self.event(
                        source_id="provider-a",
                        selection="selection-b",
                        sequence=1,
                        odds="2.10",
                    )
                )
            return captured

        with patch.object(mirror, "view", side_effect=advance_after_initial_capture):
            dependencies.register(
                "decision",
                source_ids="provider-a",
                event_ids="event-1",
                market_ids="market-1",
            )

        self.assertEqual(
            dependencies.matching_keys("decision"),
            (
                ("provider-a", "event-1|market-1|selection-a"),
                ("provider-a", "event-1|market-1|selection-b"),
            ),
        )
        view = dependencies.incremental_decision_view(
            "decision",
            as_of=datetime(2026, 9, 16, 19, 0, 10, tzinfo=timezone.utc),
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(
            [event.selection_id for event in view.events],
            ["selection-a", "selection-b"],
        )

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
