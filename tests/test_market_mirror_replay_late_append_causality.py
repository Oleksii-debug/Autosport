from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.storage import SQLiteMarketStore


class MarketMirrorReplayLateAppendCausalityTests(unittest.TestCase):
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
    def replay(cls, store: SQLiteMarketStore):
        return MarketMirror.replay_view_from_store(
            store,
            as_of=cls.CUTOFF,
            max_age=timedelta(minutes=2),
        )

    @staticmethod
    def semantic_events(snapshot) -> tuple[dict[str, object], ...]:
        return tuple(event.to_dict() for event in snapshot.events)

    def test_late_append_with_regressed_local_clocks_cannot_rewrite_frozen_cutoff(
        self,
    ) -> None:
        """A later append cannot backdate its product availability into an old decision.

        This models FORWARD_OBSERVED evidence. The second record is inserted only
        after the product has already resolved the cutoff, but carries caller/source
        local clocks earlier than that cutoff. A historical counterfactual import
        needs an explicit evidence-class/replay protocol; an unclassified append must
        not silently acquire historical availability from those timestamps alone.
        """

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

                frozen = self.replay(store)
                self.assertEqual(len(frozen.events), 1)
                self.assertEqual(frozen.events[0].sequence, 1)
                frozen_semantics = self.semantic_events(frozen)

                # This append happens after the cutoff was already evaluated, but
                # its caller-controlled local clocks regress behind that cutoff.
                # Product first-availability/commit time is not represented by
                # MarketEvent, so replay must not infer historical availability
                # from these timestamps alone.
                store.append(
                    self.event(
                        sequence=2,
                        odds="9.99",
                        observed_ts="2026-09-16T18:59:59+00:00",
                        ingest_ts="2026-09-16T18:59:59+00:00",
                    )
                )

                repeated = self.replay(store)
                self.assertEqual(
                    self.semantic_events(repeated),
                    frozen_semantics,
                    "later durable append retroactively rewrote an already-frozen as-of view",
                )
            finally:
                store.close()

    def test_restart_cannot_make_late_backdated_append_historical_truth(self) -> None:
        """Reopening the durable store must not launder the same late append."""

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
                before_late_append = self.replay(store)
                expected = self.semantic_events(before_late_append)

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
                after_restart = self.replay(reopened)
                self.assertEqual(
                    self.semantic_events(after_restart),
                    expected,
                    "restart laundered a later append into historical product availability",
                )
            finally:
                reopened.close()

    def test_legitimately_later_local_availability_stays_outside_prior_cutoff(
        self,
    ) -> None:
        """Positive control: existing observed/ingest cutoff fencing remains valid."""

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
                        odds="3.50",
                        observed_ts="2026-09-16T19:00:05+00:00",
                        ingest_ts="2026-09-16T19:00:05+00:00",
                    )
                )

                repeated = self.replay(store)
                self.assertEqual(self.semantic_events(repeated), expected)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
