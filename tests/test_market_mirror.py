from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate
from autosport.market_state_identity import PROPHETX_REST_MARKET_STATE_CONTRACT
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


    @staticmethod
    def prophetx_refresh_event(*, sequence: int, odds: str = "2.00") -> MarketEvent:
        timestamp = f"2026-09-16T19:00:{sequence:02d}+00:00"
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal(odds),
            observed_ts=timestamp,
            source_id="prophetx:sandbox",
            sequence=sequence,
            status="open",
            ingest_ts=timestamp,
            metadata={
                "provider": "prophetx",
                "environment": "sandbox",
                "transport_surface": "v3_affiliate_get_markets",
                "request_fingerprint_sha256": "a" * 64,
                "product_acquisition_sequence": sequence,
                "response_sha256": f"{sequence:x}".rjust(64, "0"),
                "snapshot_fingerprint_sha256": (
                    f"{sequence + 100:x}".rjust(64, "0")
                ),
                "sequence_authority_id": "prophetx-rest-test-authority",
                "sequence_source_id": (
                    "prophetx:sandbox:rest:v3-affiliate-get-markets"
                ),
                "semantic_state_contract": PROPHETX_REST_MARKET_STATE_CONTRACT,
            },
        )

    def test_higher_acquisition_with_same_semantic_state_is_classified_refresh(self) -> None:
        mirror = MarketMirror()
        first = self.prophetx_refresh_event(sequence=1)
        refresh = self.prophetx_refresh_event(sequence=2)

        self.assertEqual(mirror.apply(first).status, MirrorUpdate.APPLIED)
        result = mirror.apply(refresh)

        self.assertEqual(result.status, MirrorUpdate.SEMANTIC_REFRESH)
        self.assertEqual(result.previous_sequence, 1)
        self.assertEqual(result.current_sequence, 2)
        self.assertEqual(mirror.snapshot(), (refresh,))



    def test_semantic_refresh_advances_live_liveness_without_changing_price_state(self) -> None:
        mirror = MarketMirror()
        first = self.prophetx_refresh_event(sequence=1)
        refresh = self.prophetx_refresh_event(sequence=2)

        mirror.apply(first)
        self.assertEqual(mirror.apply(refresh).status, MirrorUpdate.SEMANTIC_REFRESH)

        before_refresh = mirror.active_view(
            as_of=datetime(2026, 9, 16, 19, 0, 1, 500000, tzinfo=timezone.utc),
            max_age=timedelta(milliseconds=750),
        )
        after_refresh = mirror.active_view(
            as_of=datetime(2026, 9, 16, 19, 0, 2, 500000, tzinfo=timezone.utc),
            max_age=timedelta(milliseconds=750),
        )

        self.assertEqual(before_refresh.events, ())
        self.assertEqual(after_refresh.events, (refresh,))
        self.assertEqual(after_refresh.events[0].decimal_odds, first.decimal_odds)

    def test_malformed_semantic_contract_degrades_to_material_update(self) -> None:
        mirror = MarketMirror()
        first = self.prophetx_refresh_event(sequence=1)
        malformed_payload = self.prophetx_refresh_event(sequence=2).to_dict()
        malformed_payload["metadata"]["semantic_state_contract"] = (
            "autosport.prophetx-rest-market-state.v999"
        )
        malformed = MarketEvent.from_dict(malformed_payload)

        self.assertEqual(mirror.apply(first).status, MirrorUpdate.APPLIED)
        result = mirror.apply(malformed)

        self.assertEqual(result.status, MirrorUpdate.APPLIED)
        self.assertEqual(mirror.snapshot(), (malformed,))

    def test_semantic_contract_still_reports_economic_change_as_applied(self) -> None:
        mirror = MarketMirror()
        first = self.prophetx_refresh_event(sequence=1, odds="2.00")
        changed = self.prophetx_refresh_event(sequence=2, odds="2.10")

        self.assertEqual(mirror.apply(first).status, MirrorUpdate.APPLIED)
        self.assertEqual(mirror.apply(changed).status, MirrorUpdate.APPLIED)

    def test_store_backed_mirror_entry_points_reject_store_subclasses(self) -> None:
        class Store(SQLiteMarketStore):
            pass

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "market.db")
            mirror = MarketMirror()
            event = self.event()
            try:
                with self.assertRaisesRegex(TypeError, "exact SQLiteMarketStore"):
                    mirror.persist_and_apply(store, event)
                with self.assertRaisesRegex(TypeError, "exact SQLiteMarketStore"):
                    MarketMirror.current_history_view_from_store(
                        store,
                        as_of=datetime(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc),
                        max_age=timedelta(seconds=30),
                    )
                with self.assertRaisesRegex(TypeError, "exact SQLiteMarketStore"):
                    MarketMirror.replay_view_from_store(
                        store,
                        as_of=datetime(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc),
                        max_age=timedelta(seconds=30),
                    )
                with self.assertRaisesRegex(TypeError, "exact SQLiteMarketStore"):
                    MarketMirror.from_store(store)
            finally:
                store.close()

    def test_store_rejects_market_event_subclasses_before_durable_write(self) -> None:
        class Event(MarketEvent):
            pass

        canonical = self.event()
        hostile = Event.from_dict(canonical.to_dict())
        self.assertIs(type(hostile), Event)

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                with self.assertRaisesRegex(TypeError, "exact MarketEvent"):
                    store.append_batch_accepted((hostile,))
                self.assertEqual(store.events(), ())
            finally:
                store.close()

    def test_market_mirror_rejects_market_event_subclasses(self) -> None:
        class Event(MarketEvent):
            pass

        canonical = self.event()
        hostile = Event.from_dict(canonical.to_dict())
        self.assertIs(type(hostile), Event)

        mirror = MarketMirror()
        with self.assertRaisesRegex(TypeError, "exact MarketEvent"):
            mirror.apply(hostile)

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                with self.assertRaisesRegex(TypeError, "exact MarketEvent"):
                    mirror.persist_and_apply(store, hostile)
            finally:
                store.close()

        with self.assertRaisesRegex(TypeError, "exact MarketEvent"):
            MarketMirror._from_proven_history(((hostile, 1),))

    def test_decision_boundary_rejects_datetime_and_timedelta_subclasses(self) -> None:
        class Instant(datetime):
            pass

        class Age(timedelta):
            pass

        mirror = MarketMirror()
        mirror.apply(self.event())

        with self.assertRaisesRegex(TypeError, "exact datetime"):
            mirror.active_view(
                as_of=Instant(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc),
                max_age=timedelta(seconds=30),
            )
        with self.assertRaisesRegex(TypeError, "exact timedelta"):
            mirror.active_view(
                as_of=datetime(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc),
                max_age=Age(seconds=30),
            )

    def test_focused_selectors_reject_string_subclasses(self) -> None:
        class Text(str):
            pass

        mirror = MarketMirror()
        mirror.apply(self.event())

        with self.assertRaisesRegex(TypeError, "entries must be exact strings"):
            mirror.view(source_ids=(Text("provider-a"),))
        with self.assertRaisesRegex(TypeError, "entries must be exact strings"):
            mirror.active_view(
                as_of=datetime(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc),
                max_age=timedelta(seconds=30),
                event_ids=(Text("event-1"),),
            )
        with self.assertRaisesRegex(TypeError, "entries must be exact strings"):
            mirror.view(source_ids=Text("provider-a"))

    def test_selector_rejects_string_subclasses_instead_of_iterating_characters(self) -> None:
        class SourceId(str):
            pass

        mirror = MarketMirror()
        mirror.apply(self.event())

        with self.assertRaisesRegex(TypeError, "exact string"):
            mirror.active_view(
                as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
                max_age=timedelta(minutes=5),
                source_ids=SourceId("provider-a"),
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


    def test_live_apply_rejects_ingest_before_local_observation(self) -> None:
        mirror = MarketMirror()
        inverted = self.event(
            observed_ts="2026-09-16T19:00:01+00:00",
            ingest_ts="2026-09-16T19:00:00+00:00",
        )

        with self.assertRaisesRegex(
            ValueError,
            "ingest_ts must not precede observed_ts",
        ):
            mirror.apply(inverted)

        self.assertEqual(mirror.snapshot(), ())

    def test_store_rejects_ingest_before_local_observation_without_durable_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                inverted = self.event(
                    observed_ts="2026-09-16T19:00:01+00:00",
                    ingest_ts="2026-09-16T19:00:00+00:00",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "ingest_ts must not precede observed_ts",
                ):
                    store.append_many([inverted])

                self.assertEqual(store.events(), [])
            finally:
                store.close()

    def test_equal_observed_and_ingest_instants_remain_valid(self) -> None:
        event = self.event(
            observed_ts="2026-09-16T19:00:00+00:00",
            ingest_ts="2026-09-16T20:00:00+01:00",
        )
        mirror = MarketMirror()

        result = mirror.apply(event)

        self.assertEqual(result.status, MirrorUpdate.APPLIED)
        self.assertEqual(mirror.snapshot(), (event,))


    def test_inverted_semantic_refresh_preserves_previous_live_truth(self) -> None:
        mirror = MarketMirror()
        first = self.prophetx_refresh_event(sequence=1)
        mirror.apply(first)

        payload = self.prophetx_refresh_event(sequence=2).to_dict()
        payload["observed_ts"] = "2026-09-16T19:00:02+00:00"
        payload["ingest_ts"] = "2026-09-16T19:00:01+00:00"
        inverted = MarketEvent.from_dict(payload)

        with self.assertRaisesRegex(
            ValueError,
            "ingest_ts must not precede observed_ts",
        ):
            mirror.apply(inverted)

        self.assertEqual(mirror.snapshot(), (first,))

    def test_persist_and_apply_inverted_receipt_has_no_durable_or_live_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            mirror = MarketMirror()
            try:
                inverted = self.event(
                    observed_ts="2026-09-16T19:00:01+00:00",
                    ingest_ts="2026-09-16T19:00:00+00:00",
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "ingest_ts must not precede observed_ts",
                ):
                    mirror.persist_and_apply(store, inverted)

                self.assertEqual(store.events(), [])
                self.assertEqual(mirror.snapshot(), ())
            finally:
                store.close()


    def test_live_apply_rejects_non_boolean_sequence_type_drift(self) -> None:
        mirror = MarketMirror()
        malformed = MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-09-16T19:00:00+00:00",
            source_id="provider-a",
            sequence=True,
            ingest_ts="2026-09-16T19:00:00+00:00",
        )

        with self.assertRaisesRegex(
            ValueError,
            "sequence must be a non-boolean int",
        ):
            mirror.apply(malformed)

        self.assertEqual(mirror.snapshot(), ())

    def test_live_apply_rejects_sequence_outside_sqlite_authority_range(self) -> None:
        mirror = MarketMirror()
        malformed = MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-09-16T19:00:00+00:00",
            source_id="provider-a",
            sequence=1 << 63,
            ingest_ts="2026-09-16T19:00:00+00:00",
        )

        with self.assertRaisesRegex(
            ValueError,
            "fit signed 64-bit SQLite INTEGER",
        ):
            mirror.apply(malformed)

        self.assertEqual(mirror.snapshot(), ())


if __name__ == "__main__":
    unittest.main()
