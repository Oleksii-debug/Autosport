import unittest

from autosport.domain import MarketEvent
from autosport.replay import ReplayEngine


class ReplayTimestampOrderTests(unittest.TestCase):
    @staticmethod
    def _event(
        event_id: str,
        observed_ts: str,
        sequence: int,
        *,
        ingest_ts: str | None = None,
    ) -> MarketEvent:
        payload = {
            "event_id": event_id,
            "market_id": "m",
            "selection_id": "a",
            "decimal_odds": "2.0",
            "observed_ts": observed_ts,
            "source_id": "source",
            "sequence": sequence,
        }
        if ingest_ts is not None:
            payload["ingest_ts"] = ingest_ts
        return MarketEvent.from_dict(payload)

    def test_replay_orders_mixed_offsets_by_absolute_instant(self):
        later_lexically_first = self._event(
            "later",
            "2026-01-01T00:30:00+00:00",
            1,
        )
        earlier_lexically_last = self._event(
            "earlier",
            "2026-01-01T01:00:00+01:00",
            2,
        )

        seen: list[str] = []
        ReplayEngine([later_lexically_first, earlier_lexically_last]).run(
            lambda event: seen.append(event.event_id),
            run_id="absolute-order",
        )

        self.assertEqual(seen, ["earlier", "later"])

    def test_replay_orders_by_when_event_was_actually_available(self):
        late_old_quote = self._event(
            "late-old",
            "2026-01-01T00:00:00+00:00",
            1,
            ingest_ts="2026-01-01T00:10:00+00:00",
        )
        on_time_newer_quote = self._event(
            "on-time-newer",
            "2026-01-01T00:05:00+00:00",
            2,
            ingest_ts="2026-01-01T00:05:00+00:00",
        )

        seen: list[str] = []
        ReplayEngine([late_old_quote, on_time_newer_quote]).run(
            lambda event: seen.append(event.event_id),
            run_id="causal-availability-order",
        )

        self.assertEqual(seen, ["on-time-newer", "late-old"])

    def test_replay_waits_for_later_observation_or_ingest_clock(self):
        observed_late = self._event(
            "observed-late",
            "2026-01-01T00:10:00+00:00",
            1,
            ingest_ts="2026-01-01T00:00:00+00:00",
        )
        fully_available_earlier = self._event(
            "available-earlier",
            "2026-01-01T00:05:00+00:00",
            2,
            ingest_ts="2026-01-01T00:05:00+00:00",
        )

        seen: list[str] = []
        ReplayEngine([observed_late, fully_available_earlier]).run(
            lambda event: seen.append(event.event_id),
            run_id="both-clocks-causal",
        )

        self.assertEqual(seen, ["available-earlier", "observed-late"])

    def test_replay_rejects_naive_ingest_timestamp_fail_closed(self):
        event = self._event(
            "naive-ingest",
            "2026-01-01T00:00:00+00:00",
            1,
            ingest_ts="2026-01-01T00:01:00",
        )
        with self.assertRaisesRegex(ValueError, "ingest_ts must include timezone"):
            ReplayEngine([event])

    def test_replay_rejects_naive_observed_timestamp_fail_closed(self):
        event = self._event("naive", "2026-01-01T00:00:00", 1)
        with self.assertRaisesRegex(ValueError, "must include timezone"):
            ReplayEngine([event])


if __name__ == "__main__":
    unittest.main()
