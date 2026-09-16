import tempfile
import unittest
from decimal import Decimal

from autosport.agents import AgentContext, AgentOrchestrator, MarketMirrorAgent
from autosport.domain import MarketEvent
from autosport.live_observation import observe_workspace_once
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import BoundedMirrorInvalidationBuffer
from autosport.paper import PaperBook
from autosport.providers import InMemoryProvider, ProviderQuote


_RECEIVE_TIME = "2026-09-16T23:30:00+00:00"


class MarketMirrorLiveWiringTests(unittest.TestCase):
    @staticmethod
    def event(source_id: str, *, odds: str) -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": "event-1",
                "market_id": "winner",
                "selection_id": "selection-a",
                "decimal_odds": odds,
                "observed_ts": "2026-09-16T23:29:00+00:00",
                "source_id": source_id,
                "sequence": 1,
                "source_ts": "2026-09-16T23:28:59+00:00",
                "ingest_ts": _RECEIVE_TIME,
            }
        )

    @staticmethod
    def provider(source_id: str, *, odds: str) -> InMemoryProvider:
        return InMemoryProvider(
            source_id,
            [
                ProviderQuote(
                    provider_event_id="event-1",
                    provider_market_id="winner",
                    provider_selection_id="selection-a",
                    decimal_odds=Decimal(odds),
                    observed_ts="2026-09-16T23:29:00+00:00",
                    sequence=1,
                    source_ts="2026-09-16T23:28:59+00:00",
                )
            ],
        )

    def test_decision_agent_mirror_keeps_same_quote_key_from_two_providers(self) -> None:
        context = AgentContext(PaperBook("100"))
        orchestrator = AgentOrchestrator([MarketMirrorAgent()], context)
        first = self.event("provider-a", odds="2.00")
        second = self.event("provider-b", odds="2.20")
        self.assertEqual(first.quote_key, second.quote_key)

        orchestrator.on_market_event(first)
        orchestrator.on_market_event(second)

        snapshot = context.market_mirror.snapshot()
        self.assertEqual(len(snapshot), 2)
        self.assertEqual(
            {(event.source_id, event.quote_key) for event in snapshot},
            {
                ("provider-a", first.quote_key),
                ("provider-b", second.quote_key),
            },
        )
        self.assertEqual(set(context.latest_quotes), {(event.source_id, event.quote_key) for event in snapshot})
        self.assertEqual(context.event_count, 2)

    def test_live_observation_feeds_shared_canonical_mirror_and_invalidations(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            mirror = MarketMirror()
            updates = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=8)

            first = observe_workspace_once(
                workspace,
                self.provider("provider-a", odds="2.00"),
                max_items=10,
                clock=lambda: _RECEIVE_TIME,
                mirror_updates=updates,
            )
            first_dirty = updates.drain(max_items=8)
            self.assertEqual(first.stats.accepted, 1)
            self.assertEqual(len(first.current_quotes), 1)
            self.assertEqual(first_dirty.changed_keys[0][0], "provider-a")
            self.assertFalse(first_dirty.full_refresh_required)

            second = observe_workspace_once(
                workspace,
                self.provider("provider-b", odds="2.20"),
                max_items=10,
                clock=lambda: _RECEIVE_TIME,
                mirror_updates=updates,
            )
            second_dirty = updates.drain(max_items=8)
            self.assertEqual(second.stats.accepted, 1)
            self.assertEqual(len(second.current_quotes), 1)
            self.assertEqual(second_dirty.changed_keys[0][0], "provider-b")
            self.assertFalse(second_dirty.full_refresh_required)

            snapshot = mirror.snapshot()
            self.assertEqual({event.source_id for event in snapshot}, {"provider-a", "provider-b"})
            self.assertEqual(len(snapshot), 2)


if __name__ == "__main__":
    unittest.main()
