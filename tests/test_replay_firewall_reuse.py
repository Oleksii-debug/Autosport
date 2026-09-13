import unittest

from autosport.domain import MarketEvent
from autosport.replay import FutureLeakageError, ReplayEngine, ReplayLeakageFirewall


class ReplayFirewallReuseTests(unittest.TestCase):
    @staticmethod
    def _event() -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": "event-1",
                "market_id": "winner",
                "selection_id": "alice",
                "decimal_odds": "2.0",
                "observed_ts": "2026-01-01T00:00:00+00:00",
                "source_id": "fixture",
                "sequence": 1,
            }
        )

    def test_completed_engine_rejects_second_run_before_callback(self) -> None:
        firewall = ReplayLeakageFirewall({"event-1": "alice"})
        engine = ReplayEngine([self._event()], firewall)
        engine.run(lambda _event: None, run_id="first")
        self.assertEqual(firewall.result_for("event-1"), "alice")

        seen: list[str] = []
        with self.assertRaisesRegex(FutureLeakageError, "already unlocked"):
            engine.run(lambda event: seen.append(event.event_id), run_id="second")

        self.assertEqual(seen, [])

    def test_completed_firewall_cannot_be_reused_by_fresh_engine(self) -> None:
        firewall = ReplayLeakageFirewall({"event-1": "alice"})
        ReplayEngine([self._event()], firewall).run(lambda _event: None, run_id="first")

        seen: list[str] = []
        fresh_engine = ReplayEngine([self._event()], firewall)
        with self.assertRaisesRegex(FutureLeakageError, "use a fresh firewall"):
            fresh_engine.run(lambda event: seen.append(event.event_id), run_id="second")

        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
