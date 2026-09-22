from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.storage import SQLiteMarketStore


class MarketMirrorReplayCutoffGenerationTests(unittest.TestCase):
    CUTOFF = datetime(2026, 9, 16, 19, 0, 1, tzinfo=timezone.utc)

    @staticmethod
    def event(
        *,
        sequence: int,
        odds: str,
        observed_ts: str,
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
            source_ts=observed_ts,
            ingest_ts=ingest_ts or observed_ts,
        )

    @classmethod
    def replay(
        cls,
        store: SQLiteMarketStore,
        *,
        as_of: datetime | None = None,
    ):
        return MarketMirror.replay_view_from_store(
            store,
            as_of=as_of or cls.CUTOFF,
            max_age=timedelta(minutes=2),
        )

    @staticmethod
    def semantic_events(snapshot) -> tuple[dict[str, object], ...]:
        return tuple(event.to_dict() for event in snapshot.events)

    def test_late_backdated_append_cannot_rewrite_frozen_cutoff(self) -> None:
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
                frozen = self.replay(store)
                expected = self.semantic_events(frozen)

                store.append(
                    self.event(
                        sequence=2,
                        odds="9.99",
                        observed_ts="2026-09-16T18:59:59+00:00",
                        ingest_ts="2026-09-16T18:59:59+00:00",
                    )
                )

                repeated = self.replay(store)
                self.assertEqual(self.semantic_events(repeated), expected)
                self.assertEqual(len(store.events()), 2)
            finally:
                store.close()

    def test_restart_re_resolves_original_frozen_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                store.append(
                    self.event(
                        sequence=1,
                        odds="2.00",
                        observed_ts="2026-09-16T19:00:00+00:00",
                    )
                )
                expected = self.semantic_events(self.replay(store))
                store.append(
                    self.event(
                        sequence=2,
                        odds="7.77",
                        observed_ts="2026-09-16T18:59:58+00:00",
                        ingest_ts="2026-09-16T18:59:58+00:00",
                    )
                )
            finally:
                store.close()

            reopened = SQLiteMarketStore(path)
            try:
                self.assertEqual(
                    self.semantic_events(self.replay(reopened)),
                    expected,
                )
                self.assertEqual(len(reopened.events()), 2)
            finally:
                reopened.close()

    def test_equivalent_timezone_instant_reuses_same_frozen_cutoff(self) -> None:
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
                expected = self.semantic_events(self.replay(store))
                store.append(
                    self.event(
                        sequence=2,
                        odds="8.88",
                        observed_ts="2026-09-16T18:59:57+00:00",
                        ingest_ts="2026-09-16T18:59:57+00:00",
                    )
                )

                equivalent = self.CUTOFF.astimezone(
                    timezone(timedelta(hours=2))
                )
                self.assertEqual(
                    self.semantic_events(self.replay(store, as_of=equivalent)),
                    expected,
                )
            finally:
                store.close()

    def test_new_later_cutoff_can_observe_later_append(self) -> None:
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
                first = self.replay(store)
                self.assertEqual(first.events[0].sequence, 1)

                store.append(
                    self.event(
                        sequence=2,
                        odds="3.00",
                        observed_ts="2026-09-16T18:59:59+00:00",
                        ingest_ts="2026-09-16T18:59:59+00:00",
                    )
                )
                self.assertEqual(self.replay(store).events[0].sequence, 1)

                later = self.replay(
                    store,
                    as_of=self.CUTOFF + timedelta(microseconds=1),
                )
                self.assertEqual(len(later.events), 1)
                self.assertEqual(later.events[0].sequence, 2)
                self.assertEqual(later.events[0].decimal_odds, Decimal("3.00"))
            finally:
                store.close()

    def test_missing_commit_generation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                event = self.event(
                    sequence=1,
                    odds="2.00",
                    observed_ts="2026-09-16T19:00:00+00:00",
                )
                store.append(event)
                store.connection.execute(
                    "DELETE FROM market_event_commit_order WHERE dedupe_key=?",
                    (event.dedupe_key,),
                )
                store.connection.commit()

                with self.assertRaises(ValueError):
                    self.replay(store)
            finally:
                store.close()

    def test_cutoff_generation_tamper_fails_closed(self) -> None:
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
                self.replay(store)
                store.connection.execute(
                    "UPDATE market_replay_cutoffs "
                    "SET max_append_generation=max_append_generation+100"
                )
                store.connection.commit()

                with self.assertRaises(ValueError):
                    self.replay(store)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
