from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate
from autosport.storage import SQLiteMarketStore


class MarketMirrorTests(unittest.TestCase):
    @staticmethod
    def event(
        *,
        source: str = "provider-a",
        event: str = "event-1",
        market: str = "market-1",
        selection: str = "selection-1",
        sequence: int = 1,
        odds: str = "2.00",
        status: str = "open",
        observed_ts: str = "2026-09-16T19:00:00+00:00",
        source_ts: str | None = None,
        ingest_ts: str | None = None,
        sport: str | None = None,
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event,
            market_id=market,
            selection_id=selection,
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            source_id=source,
            sequence=sequence,
            status=status,
            source_ts=source_ts,
            ingest_ts=ingest_ts or observed_ts,
            sport=sport,
        )

    def test_new_and_forward_updates_are_applied(self) -> None:
        mirror = MarketMirror()

        first = mirror.apply(self.event(sequence=1, odds="2.00"))
        second = mirror.apply(self.event(sequence=2, odds="2.10"))

        self.assertEqual(first.status, MirrorUpdate.APPLIED)
        self.assertEqual(second.status, MirrorUpdate.APPLIED)
        self.assertEqual(second.previous_sequence, 1)
        self.assertEqual(second.current_sequence, 2)
        self.assertEqual(
            mirror.get(
                "provider-a", "event-1", "market-1", "selection-1"
            ).decimal_odds,
            Decimal("2.10"),
        )

    def test_get_preserves_legacy_lookup_and_requires_explicit_sport_dimension(self) -> None:
        mirror = MarketMirror()
        legacy = self.event(sequence=1, odds="1.90")
        table_tennis = self.event(sequence=1, odds="2.10", sport="table_tennis")

        self.assertEqual(mirror.apply(legacy).status, MirrorUpdate.APPLIED)
        self.assertEqual(mirror.apply(table_tennis).status, MirrorUpdate.APPLIED)

        legacy_restored = mirror.get(
            "provider-a", "event-1", "market-1", "selection-1"
        )
        self.assertIsNotNone(legacy_restored)
        self.assertIsNone(legacy_restored.sport)
        self.assertEqual(legacy_restored.decimal_odds, Decimal("1.90"))

        sport_restored = mirror.get(
            "provider-a",
            "event-1",
            "market-1",
            "selection-1",
            sport="table_tennis",
        )
        self.assertIsNotNone(sport_restored)
        self.assertEqual(sport_restored.sport, "table_tennis")
        self.assertEqual(sport_restored.decimal_odds, Decimal("2.10"))
        self.assertIsNone(
            mirror.get(
                "provider-a",
                "event-1",
                "market-1",
                "selection-1",
                sport="soccer",
            )
        )

    def test_same_provider_local_ids_are_sport_disambiguated(self) -> None:
        mirror = MarketMirror()
        table_tennis = self.event(
            sequence=1, odds="2.10", sport="table_tennis"
        )
        soccer = self.event(sequence=1, odds="1.80", sport="soccer")

        self.assertEqual(mirror.apply(table_tennis).status, MirrorUpdate.APPLIED)
        self.assertEqual(mirror.apply(soccer).status, MirrorUpdate.APPLIED)
        self.assertEqual(len(mirror), 2)

        self.assertEqual(
            mirror.get(
                "provider-a",
                "event-1",
                "market-1",
                "selection-1",
                sport="table_tennis",
            ).decimal_odds,
            Decimal("2.10"),
        )
        self.assertEqual(
            mirror.get(
                "provider-a",
                "event-1",
                "market-1",
                "selection-1",
                sport="soccer",
            ).decimal_odds,
            Decimal("1.80"),
        )

    def test_sport_aware_lookup_survives_store_restore_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                self.assertEqual(
                    store.append_many(
                        [
                            self.event(
                                sequence=1,
                                odds="2.10",
                                sport="table_tennis",
                                observed_ts="2026-09-16T18:59:00+00:00",
                            ),
                            self.event(
                                sequence=1,
                                odds="1.80",
                                sport="soccer",
                                observed_ts="2026-09-16T18:59:01+00:00",
                            ),
                        ]
                    ),
                    2,
                )
                restored = MarketMirror.from_store(store)
                self.assertEqual(
                    restored.get(
                        "provider-a",
                        "event-1",
                        "market-1",
                        "selection-1",
                        sport="table_tennis",
                    ).sport,
                    "table_tennis",
                )
                self.assertEqual(
                    restored.get(
                        "provider-a",
                        "event-1",
                        "market-1",
                        "selection-1",
                        sport="soccer",
                    ).sport,
                    "soccer",
                )

                replay = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=5),
                )
                self.assertEqual(len(replay.events), 2)
                self.assertEqual(
                    {event.sport for event in replay.events},
                    {"soccer", "table_tennis"},
                )
            finally:
                store.close()

    def test_duplicate_sequence_is_idempotent(self) -> None:
        mirror = MarketMirror()
        event = self.event(sequence=7, odds="2.20")

        self.assertEqual(mirror.apply(event).status, MirrorUpdate.APPLIED)
        result = mirror.apply(event)

        self.assertEqual(result.status, MirrorUpdate.DUPLICATE)
        self.assertEqual(result.previous_sequence, 7)
        self.assertEqual(len(mirror), 1)

    def test_same_sequence_retry_ignores_local_receipt_clocks(self) -> None:
        mirror = MarketMirror()
        first = self.event(
            sequence=7,
            odds="2.20",
            source_ts="2026-09-16T18:59:59+00:00",
        )
        retry = MarketEvent.from_dict(
            {
                **first.to_dict(),
                "observed_ts": "2026-09-16T19:00:01+00:00",
                "ingest_ts": "2026-09-16T19:01:00+00:00",
            }
        )

        self.assertNotEqual(first.observed_ts, retry.observed_ts)
        self.assertNotEqual(first.ingest_ts, retry.ingest_ts)
        self.assertEqual(first.source_ts, retry.source_ts)
        self.assertEqual(mirror.apply(first).status, MirrorUpdate.APPLIED)
        self.assertEqual(mirror.apply(retry).status, MirrorUpdate.DUPLICATE)
        self.assertEqual(mirror.snapshot(), (first,))

    def test_same_sequence_still_conflicts_on_provider_source_time(self) -> None:
        mirror = MarketMirror()
        first = self.event(
            sequence=7,
            odds="2.20",
            source_ts="2026-09-16T18:59:59+00:00",
        )
        changed = MarketEvent.from_dict(
            {
                **first.to_dict(),
                "observed_ts": "2026-09-16T19:00:01+00:00",
                "ingest_ts": "2026-09-16T19:01:00+00:00",
                "source_ts": "2026-09-16T19:00:00+00:00",
            }
        )

        mirror.apply(first)
        with self.assertRaises(ValueError):
            mirror.apply(changed)

    def test_stale_sequence_is_ignored(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(sequence=9, odds="2.30"))

        result = mirror.apply(self.event(sequence=8, odds="2.00"))

        self.assertEqual(result.status, MirrorUpdate.STALE)
        self.assertEqual(result.current_sequence, 9)
        self.assertEqual(
            mirror.get(
                "provider-a", "event-1", "market-1", "selection-1"
            ).decimal_odds,
            Decimal("2.30"),
        )

    def test_same_sequence_with_different_payload_fails_closed(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(sequence=3, odds="2.00"))

        with self.assertRaises(ValueError):
            mirror.apply(self.event(sequence=3, odds="2.01"))

    def test_delimiter_bearing_quote_components_do_not_alias_in_mirror(self) -> None:
        mirror = MarketMirror()
        left = self.event(
            event="a|b",
            market="c",
            selection="d",
            sequence=1,
            odds="2.00",
        )
        right = self.event(
            event="a",
            market="b|c",
            selection="d",
            sequence=1,
            odds="1.90",
        )

        self.assertNotEqual(left.quote_key, right.quote_key)
        self.assertEqual(mirror.apply(left).status, MirrorUpdate.APPLIED)
        self.assertEqual(mirror.apply(right).status, MirrorUpdate.APPLIED)
        self.assertEqual(len(mirror), 2)
        self.assertEqual(
            mirror.get("provider-a", "a|b", "c", "d").decimal_odds,
            Decimal("2.00"),
        )
        self.assertEqual(
            mirror.get("provider-a", "a", "b|c", "d").decimal_odds,
            Decimal("1.90"),
        )

    def test_provider_identity_prevents_cross_provider_aliasing(self) -> None:
        mirror = MarketMirror()
        provider_a_result = mirror.apply(
            self.event(source="provider-a", sequence=1, odds="2.00")
        )
        provider_b_result = mirror.apply(
            self.event(source="provider-b", sequence=1, odds="1.90")
        )

        self.assertEqual(provider_a_result.quote_key, provider_b_result.quote_key)
        self.assertEqual(provider_a_result.source_id, "provider-a")
        self.assertEqual(provider_b_result.source_id, "provider-b")
        self.assertNotEqual(provider_a_result.source_id, provider_b_result.source_id)
        self.assertEqual(len(mirror), 2)
        self.assertEqual(
            mirror.get(
                "provider-a", "event-1", "market-1", "selection-1"
            ).decimal_odds,
            Decimal("2.00"),
        )
        self.assertEqual(
            mirror.get(
                "provider-b", "event-1", "market-1", "selection-1"
            ).decimal_odds,
            Decimal("1.90"),
        )

    def test_inactive_quotes_remain_auditable_but_are_excluded_from_active_snapshot(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(selection="open", sequence=1, status="open"))
        mirror.apply(self.event(selection="suspended", sequence=1, status="suspended"))
        mirror.apply(self.event(selection="closed", sequence=1, status="closed"))

        self.assertEqual(len(mirror.snapshot()), 3)
        active = mirror.active_snapshot(
            as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
            max_age=timedelta(minutes=5),
        )
        self.assertEqual(tuple(event.selection_id for event in active), ("open",))

    def test_unknown_status_remains_auditable_but_is_not_decision_eligible(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(selection="open", sequence=1, status="open"))
        mirror.apply(
            self.event(
                selection="provider-paused",
                sequence=1,
                status="provider-paused",
            )
        )

        self.assertEqual(len(mirror.snapshot()), 2)
        active = mirror.active_snapshot(
            as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
            max_age=timedelta(minutes=5),
        )
        self.assertEqual(tuple(event.selection_id for event in active), ("open",))

    def test_active_snapshot_excludes_future_expired_and_rejects_bad_time_ingress(self) -> None:
        mirror = MarketMirror()
        mirror.apply(
            self.event(
                selection="fresh",
                observed_ts="2026-09-16T18:59:30+00:00",
            )
        )
        mirror.apply(
            self.event(
                selection="boundary",
                observed_ts="2026-09-16T18:55:00+00:00",
            )
        )
        mirror.apply(
            self.event(
                selection="expired",
                observed_ts="2026-09-16T18:54:59+00:00",
            )
        )
        mirror.apply(
            self.event(
                selection="future",
                observed_ts="2026-09-16T19:00:01+00:00",
            )
        )
        bad_time = self.event(selection="bad-time")
        object.__setattr__(bad_time, "observed_ts", "not-a-timestamp")
        with self.assertRaisesRegex(ValueError, "observed_ts must be valid ISO-8601"):
            mirror.apply(bad_time)

        active = mirror.active_snapshot(
            as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
            max_age=timedelta(minutes=5),
        )

        self.assertEqual(
            tuple(event.selection_id for event in active),
            ("boundary", "fresh"),
        )
        self.assertEqual(len(mirror.snapshot()), 4)

    def test_active_snapshot_prefers_source_time_over_observation_time(self) -> None:
        mirror = MarketMirror()
        mirror.apply(
            self.event(
                selection="source-stale",
                observed_ts="2026-09-16T18:59:50+00:00",
                source_ts="2026-09-16T18:40:00+00:00",
            )
        )
        mirror.apply(
            self.event(
                selection="source-fresh",
                observed_ts="2026-09-16T18:40:00+00:00",
                source_ts="2026-09-16T18:59:45+00:00",
            )
        )
        mirror.apply(
            self.event(
                selection="source-future",
                observed_ts="2026-09-16T18:59:00+00:00",
                source_ts="2026-09-16T19:00:01+00:00",
            )
        )

        active = mirror.active_snapshot(
            as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
            max_age=timedelta(minutes=5),
        )

        self.assertEqual(
            tuple(event.selection_id for event in active),
            ("source-fresh",),
        )

    def test_active_snapshot_requires_aware_boundary_and_nonnegative_age(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event())

        with self.assertRaises(ValueError):
            mirror.active_snapshot(
                as_of=datetime(2026, 9, 16, 19, 0),
                max_age=timedelta(minutes=5),
            )
        with self.assertRaises(ValueError):
            mirror.active_snapshot(
                as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
                max_age=timedelta(seconds=-1),
            )

    def test_snapshot_order_is_deterministic(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(source="provider-b", selection="b", sequence=1))
        mirror.apply(self.event(source="provider-a", selection="z", sequence=1))
        mirror.apply(self.event(source="provider-a", selection="a", sequence=1))

        keys = tuple((event.source_id, event.quote_key) for event in mirror.snapshot())
        self.assertEqual(
            keys,
            (
                ("provider-a", "event-1|market-1|a"),
                ("provider-a", "event-1|market-1|z"),
                ("provider-b", "event-1|market-1|b"),
            ),
        )

    def test_from_store_replays_authoritative_history_and_sequence_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "market.db"
            store = SQLiteMarketStore(db_path)
            try:
                self.assertEqual(
                    store.append_many(
                        [
                            self.event(sequence=1, odds="2.00"),
                            self.event(sequence=2, odds="2.20"),
                        ]
                    ),
                    2,
                )
            finally:
                store.close()

            reopened_store = SQLiteMarketStore(db_path)
            try:
                restored = MarketMirror.from_store(reopened_store)
                restored_event = restored.get(
                    "provider-a", "event-1", "market-1", "selection-1"
                )
                self.assertIsNotNone(restored_event)
                self.assertEqual(restored_event.decimal_odds, Decimal("2.20"))
                self.assertEqual(restored_event.sequence, 2)

                stale = restored.apply(self.event(sequence=1, odds="1.50"))
                self.assertEqual(stale.status, MirrorUpdate.STALE)

                forward = restored.apply(self.event(sequence=3, odds="2.40"))
                self.assertEqual(forward.status, MirrorUpdate.APPLIED)
                self.assertEqual(
                    restored.get(
                        "provider-a", "event-1", "market-1", "selection-1"
                    ).decimal_odds,
                    Decimal("2.40"),
                )
            finally:
                reopened_store.close()

    def test_from_store_reconstructs_multiple_providers_from_authoritative_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "market.db"
            store = SQLiteMarketStore(db_path)
            try:
                self.assertEqual(
                    store.append_many(
                        [
                            self.event(
                                source="provider-a", sequence=4, odds="2.00"
                            ),
                            self.event(
                                source="provider-b", sequence=4, odds="1.80"
                            ),
                        ]
                    ),
                    2,
                )
            finally:
                store.close()

            reopened_store = SQLiteMarketStore(db_path)
            try:
                restored = MarketMirror.from_store(reopened_store)
                self.assertEqual(len(restored), 2)
                self.assertEqual(
                    restored.get(
                        "provider-a", "event-1", "market-1", "selection-1"
                    ).decimal_odds,
                    Decimal("2.00"),
                )
                self.assertEqual(
                    restored.get(
                        "provider-b", "event-1", "market-1", "selection-1"
                    ).decimal_odds,
                    Decimal("1.80"),
                )
            finally:
                reopened_store.close()

    def test_replay_view_reconstructs_pre_update_state_without_future_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append_many(
                    [
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T18:59:00+00:00",
                            source_ts="2026-09-16T18:58:55+00:00",
                            ingest_ts="2026-09-16T18:59:01+00:00",
                        ),
                        self.event(
                            sequence=2,
                            odds="9.99",
                            observed_ts="2026-09-16T19:01:00+00:00",
                            source_ts="2026-09-16T18:59:30+00:00",
                            ingest_ts="2026-09-16T19:01:01+00:00",
                        ),
                    ]
                )

                replay = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=5),
                )

                self.assertEqual(replay.revision, 1)
                self.assertEqual(len(replay.events), 1)
                self.assertEqual(replay.events[0].sequence, 1)
                self.assertEqual(replay.events[0].decimal_odds, Decimal("2.00"))
            finally:
                store.close()

    def test_replay_view_excludes_late_ingestion_even_when_provider_evidence_is_earlier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append_many(
                    [
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T18:58:30+00:00",
                            source_ts="2026-09-16T18:58:20+00:00",
                            ingest_ts="2026-09-16T18:58:40+00:00",
                        ),
                        self.event(
                            sequence=2,
                            odds="9.99",
                            observed_ts="2026-09-16T18:59:20+00:00",
                            source_ts="2026-09-16T18:59:10+00:00",
                            ingest_ts="2026-09-16T19:01:00+00:00",
                        ),
                    ]
                )

                replay = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=5),
                )

                self.assertEqual(replay.revision, 1)
                self.assertEqual(len(replay.events), 1)
                self.assertEqual(replay.events[0].sequence, 1)
                self.assertEqual(replay.events[0].decimal_odds, Decimal("2.00"))
            finally:
                store.close()

    def test_replay_view_applies_live_freshness_and_focused_selectors_at_one_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append_many(
                    [
                        self.event(
                            source="provider-a",
                            selection="wanted",
                            observed_ts="2026-09-16T18:59:30+00:00",
                        ),
                        self.event(
                            source="provider-a",
                            selection="stale",
                            observed_ts="2026-09-16T18:40:00+00:00",
                        ),
                        self.event(
                            source="provider-b",
                            selection="other",
                            observed_ts="2026-09-16T18:59:40+00:00",
                        ),
                        self.event(
                            source="provider-a",
                            selection="future",
                            observed_ts="2026-09-16T19:00:01+00:00",
                        ),
                    ]
                )

                replay = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=5),
                    source_ids="provider-a",
                    selection_ids={"wanted", "stale", "future"},
                )

                self.assertEqual(replay.revision, 3)
                self.assertEqual(
                    tuple(event.selection_id for event in replay.events),
                    ("wanted",),
                )
            finally:
                store.close()


    def test_apply_revalidates_post_construction_identity_before_hashing(self) -> None:
        hash_calls: list[str] = []

        class HostileIdentity(str):
            def __hash__(self) -> int:
                hash_calls.append("hash")
                raise AssertionError("mutated identity reached hash dispatch")

            def __eq__(self, other: object) -> bool:
                raise AssertionError("mutated identity reached equality dispatch")

        event = self.event(sequence=90)
        object.__setattr__(event, "source_id", HostileIdentity(event.source_id))

        mirror = MarketMirror()
        with self.assertRaises(ValueError):
            mirror.apply(event)
        self.assertEqual(hash_calls, [])
        self.assertEqual(len(mirror), 0)

    def test_apply_rejects_market_event_subclass_before_live_dispatch(self) -> None:
        class HostileMarketEvent(MarketEvent):
            def __getattribute__(self, name: str):
                if name in {"source_id", "quote_key", "sequence", "to_dict"}:
                    raise AssertionError("MarketEvent subtype dispatch must not execute")
                return super().__getattribute__(name)

        hostile = object.__new__(HostileMarketEvent)

        mirror = MarketMirror()
        with self.assertRaisesRegex(TypeError, "exact MarketEvent"):
            mirror.apply(hostile)
        self.assertEqual(len(mirror), 0)

    def test_persist_and_apply_rejects_market_event_subclass_before_store_use(self) -> None:
        class HostileMarketEvent(MarketEvent):
            def __getattribute__(self, name: str):
                if name in {"source_id", "quote_key", "sequence", "to_dict"}:
                    raise AssertionError("MarketEvent subtype dispatch must not execute")
                return super().__getattribute__(name)

        hostile = object.__new__(HostileMarketEvent)

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            mirror = MarketMirror()
            try:
                with self.assertRaisesRegex(TypeError, "exact MarketEvent"):
                    mirror.persist_and_apply(store, hostile)
                self.assertEqual(store.events(), [])
                self.assertEqual(len(mirror), 0)
            finally:
                store.close()

    def test_persist_and_apply_rejects_store_subclass_before_append_dispatch(self) -> None:
        class HostileStore(SQLiteMarketStore):
            def append(self, event: MarketEvent) -> bool:
                raise AssertionError("store subclass append dispatch must not execute")

        hostile = object.__new__(HostileStore)
        mirror = MarketMirror()
        with self.assertRaisesRegex(TypeError, "exact SQLiteMarketStore"):
            mirror.persist_and_apply(hostile, self.event(sequence=93))
        self.assertEqual(len(mirror), 0)

    def test_get_rejects_identity_subclasses_before_lookup_dispatch(self) -> None:
        dispatch_calls: list[str] = []

        class HostileIdentity(str):
            def strip(self, *args: object, **kwargs: object) -> str:
                dispatch_calls.append("strip")
                raise AssertionError("identity strip dispatched before exact-type admission")

            def encode(self, *args: object, **kwargs: object) -> bytes:
                dispatch_calls.append("encode")
                raise AssertionError("identity encode dispatched before exact-type admission")

            def __format__(self, spec: str) -> str:
                dispatch_calls.append("format")
                raise AssertionError("identity formatting dispatched before exact-type admission")

            def __hash__(self) -> int:
                dispatch_calls.append("hash")
                raise AssertionError("identity hash dispatched before exact-type admission")

            def __eq__(self, other: object) -> bool:
                dispatch_calls.append("eq")
                raise AssertionError("identity equality dispatched before exact-type admission")

        canonical = {
            "source_id": "provider-a",
            "event_id": "event-1",
            "market_id": "market-1",
            "selection_id": "selection-1",
        }
        mirror = MarketMirror()
        for field_name in canonical:
            values = dict(canonical)
            values[field_name] = HostileIdentity(values[field_name])
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValueError):
                    mirror.get(**values)
                self.assertEqual(dispatch_calls, [])

    def test_view_rejects_str_subclass_selector_before_hash_dispatch(self) -> None:
        hash_calls: list[str] = []

        class HostileSelector(str):
            def __hash__(self) -> int:
                hash_calls.append("hash")
                raise AssertionError("selector hash dispatched before exact-type admission")

            def __eq__(self, other: object) -> bool:
                raise AssertionError("selector equality dispatched before exact-type admission")

        mirror = MarketMirror()
        hostile = HostileSelector("provider-a")
        for selector in (hostile, [hostile]):
            with self.subTest(selector_type=type(selector).__name__):
                with self.assertRaisesRegex(
                    ValueError,
                    "source_ids entries must be non-empty strings",
                ):
                    mirror.view(source_ids=selector)
                self.assertEqual(hash_calls, [])


    def test_lookup_boundaries_reject_control_and_non_utf8_identity_aliases(self) -> None:
        mirror = MarketMirror()
        invalid_values = (
            "provider\x00a",
            "provider\na",
            "provider\x7fa",
            "provider\ud800a",
        )
        for invalid in invalid_values:
            with self.subTest(api="event_for_quote_key", value=repr(invalid)):
                with self.assertRaises(ValueError):
                    mirror.event_for_quote_key(invalid, "event|market|selection")
                with self.assertRaises(ValueError):
                    mirror.event_for_quote_key("provider-a", invalid)

            with self.subTest(api="active_view_for_keys", value=repr(invalid)):
                with self.assertRaises(ValueError):
                    mirror.active_view_for_keys(
                        ((invalid, "event|market|selection"),),
                        as_of=datetime(2026, 10, 7, tzinfo=timezone.utc),
                        max_age=timedelta(minutes=5),
                    )
                with self.assertRaises(ValueError):
                    mirror.active_view_for_keys(
                        (("provider-a", invalid),),
                        as_of=datetime(2026, 10, 7, tzinfo=timezone.utc),
                        max_age=timedelta(minutes=5),
                    )

    def test_view_selectors_reject_control_and_non_utf8_identity_aliases(self) -> None:
        mirror = MarketMirror()
        for invalid in ("provider\x00a", "provider\na", "provider\x7fa", "provider\ud800a"):
            with self.subTest(value=repr(invalid)):
                with self.assertRaises(ValueError):
                    mirror.view(source_ids=(invalid,))



if __name__ == "__main__":
    unittest.main()
