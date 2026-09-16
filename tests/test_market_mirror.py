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

    def test_active_snapshot_excludes_future_expired_and_bad_time_entries(self) -> None:
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
        mirror.apply(
            self.event(
                selection="bad-time",
                observed_ts="not-a-timestamp",
            )
        )

        active = mirror.active_snapshot(
            as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
            max_age=timedelta(minutes=5),
        )

        self.assertEqual(
            tuple(event.selection_id for event in active),
            ("boundary", "fresh"),
        )
        self.assertEqual(len(mirror.snapshot()), 5)

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


if __name__ == "__main__":
    unittest.main()
