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

    def test_lazy_batch_producer_cannot_rewrite_already_persisted_delivery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                bus = MarketEventBus(store)
                first = MarketEvent.from_dict(
                    {
                        "event_id": "event-1",
                        "market_id": "winner",
                        "selection_id": "alice",
                        "decimal_odds": "2.0",
                        "observed_ts": "2026-09-14T00:00:00+00:00",
                        "source_id": "source-1",
                        "sequence": 1,
                        "metadata": {"nested": {"origin": "first-durable"}},
                    }
                )
                second = MarketEvent.from_dict(
                    {
                        "event_id": "event-1",
                        "market_id": "winner",
                        "selection_id": "bob",
                        "decimal_odds": "2.5",
                        "observed_ts": "2026-09-14T00:00:01+00:00",
                        "source_id": "source-1",
                        "sequence": 2,
                        "metadata": {"nested": {"origin": "second-durable"}},
                    }
                )

                def mutate_after_first_yield():
                    yield first
                    first.metadata["nested"]["origin"] = "producer-mutated-after-persistence"
                    first.metadata["producer_only"] = True
                    yield second

                observed: list[tuple[int, dict[str, object]]] = []

                def observe_then_fail(delivered: MarketEvent) -> None:
                    observed.append((delivered.sequence, deepcopy(delivered.metadata)))
                    if delivered.sequence == 1:
                        raise RuntimeError("first delivery failed")

                bus.subscribe(observe_then_fail)

                with self.assertRaises(MarketEventDeliveryError) as raised:
                    bus.publish_many(mutate_after_first_yield())

                durable = {event.sequence: event.metadata for event in store.events()}
                self.assertEqual(
                    durable,
                    {
                        1: {"nested": {"origin": "first-durable"}},
                        2: {"nested": {"origin": "second-durable"}},
                    },
                )
                self.assertEqual(
                    observed,
                    [
                        (1, {"nested": {"origin": "first-durable"}}),
                        (2, {"nested": {"origin": "second-durable"}}),
                    ],
                )
                self.assertEqual(
                    raised.exception.accepted_events[0].metadata,
                    {"nested": {"origin": "first-durable"}},
                )
                self.assertEqual(raised.exception.accepted_count, 2)
                self.assertEqual(len(raised.exception.exceptions), 1)
                self.assertEqual(
                    first.metadata,
                    {
                        "nested": {"origin": "producer-mutated-after-persistence"},
                        "producer_only": True,
                    },
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
