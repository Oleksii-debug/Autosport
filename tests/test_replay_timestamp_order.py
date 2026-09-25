import unittest

from autosport.domain import MarketEvent
from autosport.replay import ReplayEngine


class ReplayTimestampOrderTests(unittest.TestCase):
    @staticmethod
    def _event(event_id: str, observed_ts: str, sequence: int) -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": event_id,
                "market_id": "m",
                "selection_id": "a",
                "decimal_odds": "2.0",
                "observed_ts": observed_ts,
                "source_id": "source",
                "sequence": sequence,
            }
        )

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

    def test_replay_rejects_naive_observed_timestamp_fail_closed(self):
        event = self._event("naive", "2026-01-01T00:00:00", 1)
        with self.assertRaisesRegex(ValueError, "must include timezone"):
            ReplayEngine([event])


if __name__ == "__main__":
    unittest.main()
