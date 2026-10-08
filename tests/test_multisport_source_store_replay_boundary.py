from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent, MarketType
from autosport.market_mirror import MarketMirror
from autosport.replay import ReplayEngine
from autosport.storage import SQLiteMarketStore


class MultiSportSourceStoreReplayBoundaryTests(unittest.TestCase):
    SOURCE_ID = "test-only-conformance-source"
    EVENT_ID = "same-provider-event"
    MARKET_ID = "same-provider-market"
    SELECTION_ID = "same-selection"

    @staticmethod
    def _instant(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )

    @classmethod
    def _event(
        cls,
        *,
        sport: str,
        sequence: int,
        odds: str,
        observed_ts: str,
        ingest_ts: str,
        market_type: MarketType = MarketType.WINNER,
        market_semantics_id: str = "match_odds",
        evidence_grade: str = "TEST_ONLY_ENGINEERING_CONFORMANCE",
    ) -> MarketEvent:
        return MarketEvent(
            event_id=cls.EVENT_ID,
            market_id=cls.MARKET_ID,
            selection_id=cls.SELECTION_ID,
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            source_id=cls.SOURCE_ID,
            sequence=sequence,
            market_type=market_type,
            status="open",
            source_ts=observed_ts,
            ingest_ts=ingest_ts,
            sport=sport,
            market_semantics_id=market_semantics_id,
            provider_source_class="test_only_engineering_conformance",
            metadata={"evidence_grade": evidence_grade},
        )

    def test_sport_identity_causal_cutoff_and_restart_remain_bound(self) -> None:
        table_tennis = self._event(
            sport="table_tennis",
            sequence=1,
            odds="2.10",
            observed_ts="2026-09-22T00:00:01Z",
            ingest_ts="2026-09-22T00:00:01Z",
        )
        soccer_initial = self._event(
            sport="soccer",
            sequence=1,
            odds="3.00",
            observed_ts="2026-09-22T00:00:02Z",
            ingest_ts="2026-09-22T00:00:02Z",
        )
        soccer_late_correction = self._event(
            sport="soccer",
            sequence=2,
            odds="3.25",
            observed_ts="2026-09-22T00:00:03Z",
            ingest_ts="2026-09-22T00:10:00Z",
        )

        self.assertNotEqual(table_tennis.quote_key, soccer_initial.quote_key)
        self.assertNotEqual(table_tennis.dedupe_key, soccer_initial.dedupe_key)

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "multi sport replay.db"
            store = SQLiteMarketStore(db_path)
            raw_events: list[MarketEvent] = []
            strategy_events: list[MarketEvent] = []

            def persist_raw(event: MarketEvent) -> None:
                raw_events.append(event)
                store.append(event)

            engine = ReplayEngine(
                [soccer_late_correction, table_tennis, soccer_initial]
            )
            run = engine.run(
                strategy_events.append,
                on_raw_event=persist_raw,
            )

            self.assertEqual(run.event_count, 3)
            self.assertEqual(len(raw_events), 3)
            self.assertEqual(len(strategy_events), 3)
            self.assertEqual(
                {event.sport for event in strategy_events},
                {"table_tennis", "soccer"},
            )
            self.assertEqual(strategy_events[-1].sequence, 2)
            self.assertEqual(strategy_events[-1].sport, "soccer")

            early = MarketMirror.replay_view_from_store(
                store,
                as_of=self._instant("2026-09-22T00:05:00Z"),
                max_age=timedelta(hours=1),
            )
            early_by_sport = {event.sport: event for event in early.events}
            self.assertEqual(set(early_by_sport), {"table_tennis", "soccer"})
            self.assertEqual(early_by_sport["soccer"].sequence, 1)
            self.assertEqual(
                early_by_sport["soccer"].decimal_odds,
                Decimal("3.00"),
            )

            late = MarketMirror.replay_view_from_store(
                store,
                as_of=self._instant("2026-09-22T00:15:00Z"),
                max_age=timedelta(hours=1),
            )
            late_by_sport = {event.sport: event for event in late.events}
            self.assertEqual(set(late_by_sport), {"table_tennis", "soccer"})
            self.assertEqual(late_by_sport["soccer"].sequence, 2)
            self.assertEqual(
                late_by_sport["soccer"].decimal_odds,
                Decimal("3.25"),
            )

            current = store.current_by_source()
            self.assertEqual(len(current), 2)
            self.assertEqual(
                {event.sport for event in current.values()},
                {"table_tennis", "soccer"},
            )
            original_dataset_hash = engine.dataset_hash
            store.close()

            reopened = SQLiteMarketStore(db_path)
            try:
                replayed = reopened.events()
                self.assertEqual(len(replayed), 3)
                self.assertEqual(
                    [event.sport for event in replayed].count("soccer"),
                    2,
                )
                self.assertEqual(
                    ReplayEngine(replayed).dataset_hash,
                    original_dataset_hash,
                )
                soccer_rows = [
                    event for event in replayed if event.sport == "soccer"
                ]
                self.assertTrue(
                    all(
                        event.metadata.get("evidence_grade")
                        == "TEST_ONLY_ENGINEERING_CONFORMANCE"
                        for event in soccer_rows
                    )
                )

                relabelled_same_identity = self._event(
                    sport="soccer",
                    sequence=1,
                    odds="3.00",
                    observed_ts="2026-09-22T00:00:02Z",
                    ingest_ts="2026-09-22T00:00:02Z",
                    evidence_grade="EXTERNAL_PROVIDER_OBSERVATION",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "conflicting duplicate market event identity",
                ):
                    reopened.append(relabelled_same_identity)
            finally:
                reopened.close()

    def test_higher_sequence_cannot_rebind_market_semantics_inside_one_sport_stream(
        self,
    ) -> None:
        winner = self._event(
            sport="soccer",
            sequence=10,
            odds="2.40",
            observed_ts="2026-09-22T01:00:00Z",
            ingest_ts="2026-09-22T01:00:00Z",
            market_type=MarketType.WINNER,
            market_semantics_id="match_odds",
        )
        semantic_rebind = self._event(
            sport="soccer",
            sequence=11,
            odds="2.50",
            observed_ts="2026-09-22T01:00:01Z",
            ingest_ts="2026-09-22T01:00:01Z",
            market_type=MarketType.TOTAL,
            market_semantics_id="total_points",
        )

        self.assertEqual(winner.quote_key, semantic_rebind.quote_key)
        self.assertNotEqual(winner.market_type, semantic_rebind.market_type)
        self.assertNotEqual(
            winner.market_semantics_id,
            semantic_rebind.market_semantics_id,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMarketStore(Path(temp_dir) / "semantic-rebind.db")
            try:
                with self.assertRaises(
                    ValueError,
                    msg=(
                        "a higher provider sequence must not silently rebind the "
                        "market semantic identity of one sport-bound quote stream"
                    ),
                ):
                    ReplayEngine([winner, semantic_rebind]).run(
                        lambda _event: None,
                        on_raw_event=store.append,
                    )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
