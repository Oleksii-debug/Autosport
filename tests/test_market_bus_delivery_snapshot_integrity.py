from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.market_bus import MarketEventBus, MarketEventDeliveryError
from autosport.storage import SQLiteMarketStore


class MarketEventBusDeliverySnapshotIntegrityTests(unittest.TestCase):
    def test_subscriber_mutation_cannot_rewrite_later_delivery_or_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                bus = MarketEventBus(store)
                event = MarketEvent.from_dict(
                    {
                        "event_id": "event-1",
                        "market_id": "winner",
                        "selection_id": "alice",
                        "decimal_odds": "2.0",
                        "observed_ts": "2026-09-14T00:00:00+00:00",
                        "source_id": "source-1",
                        "sequence": 1,
                        "metadata": {"nested": {"origin": "durable"}},
                    }
                )
                observed: list[dict[str, object]] = []

                def mutate_then_fail(delivered: MarketEvent) -> None:
                    delivered.metadata["nested"]["origin"] = "subscriber-mutated"
                    delivered.metadata["subscriber_only"] = True
                    raise RuntimeError("subscriber failed after mutation")

                def observe(delivered: MarketEvent) -> None:
                    observed.append(deepcopy(delivered.metadata))
                    delivered.metadata["nested"]["origin"] = "observer-mutated"

                bus.subscribe(mutate_then_fail)
                bus.subscribe(observe)

                with self.assertRaises(MarketEventDeliveryError) as raised:
                    bus.publish(event)

                expected = {"nested": {"origin": "durable"}}
                self.assertEqual(observed, [expected])
                self.assertEqual(event.metadata, expected)
                self.assertEqual(store.events()[0].metadata, expected)
                self.assertEqual(raised.exception.accepted_events[0].metadata, expected)
                self.assertEqual(raised.exception.accepted_count, 1)
                self.assertEqual(len(raised.exception.exceptions), 1)
                self.assertIsInstance(raised.exception.exceptions[0], RuntimeError)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
