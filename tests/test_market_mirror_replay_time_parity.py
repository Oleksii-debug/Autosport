from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.storage import SQLiteMarketStore


class MarketMirrorReplayTimeParityTests(unittest.TestCase):
    @staticmethod
    def event(
        *,
        sequence: int,
        odds: str,
        observed_ts: str,
        source_ts: str | None = None,
        ingest_ts: str | None = None,
    ) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            source_id="provider-a",
            sequence=sequence,
            status="open",
            source_ts=source_ts or observed_ts,
            ingest_ts=ingest_ts or observed_ts,
        )

    def test_replay_rewind_does_not_leak_state_from_later_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append_many(
                    [
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        ),
                        self.event(
                            sequence=2,
                            odds="2.40",
                            observed_ts="2026-09-16T19:00:02+00:00",
                        ),
                    ]
                )
                later = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 9, 16, 19, 0, 3, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=1),
                )
                rewind = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=1),
                )
                repeated = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=1),
                )

                self.assertEqual(later.events[0].sequence, 2)
                self.assertEqual(later.events[0].decimal_odds, Decimal("2.40"))
                self.assertEqual(rewind.events[0].sequence, 1)
                self.assertEqual(rewind.events[0].decimal_odds, Decimal("2.00"))
                self.assertEqual(
                    tuple(event.to_dict() for event in rewind.events),
                    tuple(event.to_dict() for event in repeated.events),
                )
            finally:
                store.close()

    def test_equivalent_timezone_offsets_resolve_identical_replay_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                utc_view = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=1),
                )
                offset_view = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(
                        2026,
                        9,
                        16,
                        21,
                        0,
                        1,
                        tzinfo=timezone(timedelta(hours=2)),
                    ),
                    max_age=timedelta(minutes=1),
                )

                self.assertEqual(utc_view.revision, offset_view.revision)
                self.assertEqual(
                    tuple(event.to_dict() for event in utc_view.events),
                    tuple(event.to_dict() for event in offset_view.events),
                )
            finally:
                store.close()

    def test_replay_rejects_naive_decision_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                with self.assertRaisesRegex(ValueError, "timezone-aware"):
                    MarketMirror.replay_view_from_store(
                        store,
                        as_of=datetime(2026, 9, 16, 19, 0, 1),
                        max_age=timedelta(minutes=1),
                    )
            finally:
                store.close()

    def test_subsecond_cutoff_includes_equal_and_excludes_later_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append_many(
                    [
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00.499999+00:00",
                        ),
                        self.event(
                            sequence=2,
                            odds="2.10",
                            observed_ts="2026-09-16T19:00:00.500000+00:00",
                        ),
                        self.event(
                            sequence=3,
                            odds="9.99",
                            observed_ts="2026-09-16T19:00:00.500001+00:00",
                        ),
                    ]
                )
                view = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(
                        2026, 9, 16, 19, 0, 0, 500000, tzinfo=timezone.utc
                    ),
                    max_age=timedelta(seconds=1),
                )

                self.assertEqual(len(view.events), 1)
                self.assertEqual(view.events[0].sequence, 2)
                self.assertEqual(view.events[0].decimal_odds, Decimal("2.10"))
            finally:
                store.close()

    def test_old_provider_timestamp_cannot_backdate_later_product_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append_many(
                    [
                        self.event(
                            sequence=1,
                            odds="2.00",
                            observed_ts="2026-09-16T19:00:00+00:00",
                        ),
                        self.event(
                            sequence=2,
                            odds="2.80",
                            source_ts="2026-09-16T18:59:30+00:00",
                            observed_ts="2026-09-16T19:00:05+00:00",
                            ingest_ts="2026-09-16T19:00:05+00:00",
                        ),
                    ]
                )
                before_receipt = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=2),
                )
                after_receipt = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(2026, 9, 16, 19, 0, 6, tzinfo=timezone.utc),
                    max_age=timedelta(minutes=2),
                )

                self.assertEqual(before_receipt.events[0].sequence, 1)
                self.assertEqual(after_receipt.events[0].sequence, 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
