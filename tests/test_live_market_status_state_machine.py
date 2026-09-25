from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate
from autosport.storage import SQLiteMarketStore


class LiveMarketStatusStateMachineTests(unittest.TestCase):
    SOURCE = "provider-status"
    EVENT = "event-status"
    MARKET = "winner"
    SELECTION = "home"

    @staticmethod
    def _event(
        *,
        sequence: int,
        status: str,
        observed_ts: str,
        odds: str = "2.00",
    ) -> MarketEvent:
        return MarketEvent(
            event_id=LiveMarketStatusStateMachineTests.EVENT,
            market_id=LiveMarketStatusStateMachineTests.MARKET,
            selection_id=LiveMarketStatusStateMachineTests.SELECTION,
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            source_id=LiveMarketStatusStateMachineTests.SOURCE,
            sequence=sequence,
            status=status,
            source_ts=observed_ts,
            ingest_ts=observed_ts,
            sport="table_tennis",
        )

    @staticmethod
    def _active(mirror: MarketMirror, at: str) -> tuple[MarketEvent, ...]:
        return mirror.active_snapshot(
            as_of=datetime.fromisoformat(at).astimezone(timezone.utc),
            max_age=timedelta(minutes=10),
        )

    def test_suspension_blocks_stale_and_same_sequence_reopen(self) -> None:
        mirror = MarketMirror()
        opened = self._event(
            sequence=10,
            status="open",
            observed_ts="2026-09-21T08:00:00+00:00",
        )
        suspended = self._event(
            sequence=11,
            status="suspended",
            observed_ts="2026-09-21T08:00:01+00:00",
        )

        self.assertEqual(mirror.apply(opened).status, MirrorUpdate.APPLIED)
        self.assertEqual(mirror.apply(suspended).status, MirrorUpdate.APPLIED)
        self.assertEqual(
            self._active(mirror, "2026-09-21T08:00:02+00:00"),
            (),
        )

        stale_open = self._event(
            sequence=10,
            status="open",
            observed_ts="2026-09-21T08:00:02+00:00",
            odds="2.10",
        )
        stale = mirror.apply(stale_open)
        self.assertEqual(stale.status, MirrorUpdate.STALE)
        self.assertEqual(
            mirror.get(
                self.SOURCE,
                self.EVENT,
                self.MARKET,
                self.SELECTION,
                sport="table_tennis",
            ).status,
            "suspended",
        )

        same_sequence_open = self._event(
            sequence=11,
            status="open",
            observed_ts="2026-09-21T08:00:01+00:00",
        )
        with self.assertRaisesRegex(
            ValueError,
            "conflicting MarketEvent payload reused an existing source-local sequence",
        ):
            mirror.apply(same_sequence_open)
        self.assertEqual(
            self._active(mirror, "2026-09-21T08:00:03+00:00"),
            (),
        )

    def test_only_strictly_newer_open_observation_reopens_suspended_quote(self) -> None:
        mirror = MarketMirror()
        mirror.apply(
            self._event(
                sequence=20,
                status="open",
                observed_ts="2026-09-21T08:01:00+00:00",
            )
        )
        mirror.apply(
            self._event(
                sequence=21,
                status="suspended",
                observed_ts="2026-09-21T08:01:01+00:00",
            )
        )
        reopened = self._event(
            sequence=22,
            status="open",
            observed_ts="2026-09-21T08:01:02+00:00",
            odds="2.05",
        )

        result = mirror.apply(reopened)

        self.assertEqual(result.status, MirrorUpdate.APPLIED)
        active = self._active(mirror, "2026-09-21T08:01:03+00:00")
        self.assertEqual(active, (reopened,))

    def test_closed_status_remains_decision_ineligible_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.db"
            store = SQLiteMarketStore(path)
            try:
                mirror = MarketMirror()
                mirror.persist_and_apply(
                    store,
                    self._event(
                        sequence=30,
                        status="open",
                        observed_ts="2026-09-21T08:02:00+00:00",
                    ),
                )
                closed = self._event(
                    sequence=31,
                    status="closed",
                    observed_ts="2026-09-21T08:02:01+00:00",
                )
                mirror.persist_and_apply(store, closed)

                restored = MarketMirror.from_store(store)
                current = restored.get(
                    self.SOURCE,
                    self.EVENT,
                    self.MARKET,
                    self.SELECTION,
                    sport="table_tennis",
                )
                self.assertIsNotNone(current)
                self.assertEqual(current.status, "closed")
                self.assertEqual(
                    self._active(restored, "2026-09-21T08:02:02+00:00"),
                    (),
                )
            finally:
                store.close()

    def test_replay_does_not_leak_future_reopen_across_suspension(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                store.append(
                    self._event(
                        sequence=40,
                        status="open",
                        observed_ts="2026-09-21T08:03:00+00:00",
                    )
                )
                store.append(
                    self._event(
                        sequence=41,
                        status="suspended",
                        observed_ts="2026-09-21T08:03:10+00:00",
                    )
                )
                reopened = self._event(
                    sequence=42,
                    status="open",
                    observed_ts="2026-09-21T08:03:20+00:00",
                    odds="2.20",
                )
                store.append(reopened)

                during_suspension = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(
                        2026, 9, 21, 8, 3, 15, tzinfo=timezone.utc
                    ),
                    max_age=timedelta(minutes=10),
                )
                after_reopen = MarketMirror.replay_view_from_store(
                    store,
                    as_of=datetime(
                        2026, 9, 21, 8, 3, 25, tzinfo=timezone.utc
                    ),
                    max_age=timedelta(minutes=10),
                )

                self.assertEqual(during_suspension.events, ())
                self.assertEqual(after_reopen.events, (reopened,))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
