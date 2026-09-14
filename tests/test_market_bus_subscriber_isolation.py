import tempfile
import unittest
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.market_bus import MarketEventBus, MarketEventDeliveryError
from autosport.storage import SQLiteMarketStore


class MarketEventBusSubscriberIsolationTests(unittest.TestCase):
    @staticmethod
    def _event(sequence: int) -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": "event-1",
                "market_id": "winner",
                "selection_id": "alice",
                "decimal_odds": "2.0",
                "observed_ts": f"2026-01-01T00:00:0{sequence}+00:00",
                "source_id": "source-1",
                "sequence": sequence,
            }
        )

    def test_failing_subscriber_does_not_starve_later_subscriber(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                bus = MarketEventBus(store)
                received: list[MarketEvent] = []

                def fail(_event: MarketEvent) -> None:
                    raise RuntimeError("subscriber boom")

                bus.subscribe(fail)
                bus.subscribe(received.append)
                event = self._event(1)

                with self.assertRaises(MarketEventDeliveryError) as raised:
                    bus.publish(event)

                self.assertEqual(len(raised.exception.exceptions), 1)
                self.assertIsInstance(raised.exception.exceptions[0], RuntimeError)
                self.assertEqual(raised.exception.accepted_events, (event,))
                self.assertEqual(raised.exception.accepted_count, 1)
                self.assertEqual(received, [event])
                self.assertEqual(store.events(), [event])

                # Persistence already succeeded. Dedupe prevents a retry from
                # duplicating delivery to subscribers that already succeeded.
                self.assertFalse(bus.publish(event))
                self.assertEqual(received, [event])
            finally:
                store.close()

    def test_batch_failure_does_not_starve_later_events_or_subscribers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                bus = MarketEventBus(store)
                attempted: list[int] = []
                received: list[int] = []

                def flaky(event: MarketEvent) -> None:
                    attempted.append(event.sequence)
                    if event.sequence == 1:
                        raise ValueError("first event failed")

                bus.subscribe(flaky)
                bus.subscribe(lambda event: received.append(event.sequence))
                events = [self._event(1), self._event(2)]

                with self.assertRaises(MarketEventDeliveryError) as raised:
                    bus.publish_many(events)

                self.assertEqual(len(raised.exception.exceptions), 1)
                self.assertIsInstance(raised.exception.exceptions[0], ValueError)
                self.assertEqual(raised.exception.accepted_events, tuple(events))
                self.assertEqual(raised.exception.accepted_count, 2)
                self.assertEqual(attempted, [1, 2])
                self.assertEqual(received, [1, 2])
                self.assertEqual([event.sequence for event in store.events()], [1, 2])
            finally:
                store.close()

    def test_collects_all_callback_failures_in_deterministic_attempt_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                bus = MarketEventBus(store)
                attempts: list[tuple[str, int]] = []

                def first(event: MarketEvent) -> None:
                    attempts.append(("first", event.sequence))
                    if event.sequence == 1:
                        raise ValueError("first/1")

                def second(event: MarketEvent) -> None:
                    attempts.append(("second", event.sequence))
                    raise RuntimeError(f"second/{event.sequence}")

                def third(event: MarketEvent) -> None:
                    attempts.append(("third", event.sequence))

                bus.subscribe(first)
                bus.subscribe(second)
                bus.subscribe(third)
                events = [self._event(1), self._event(2)]

                with self.assertRaises(MarketEventDeliveryError) as raised:
                    bus.publish_many(events)

                self.assertEqual(
                    attempts,
                    [
                        ("first", 1),
                        ("second", 1),
                        ("third", 1),
                        ("first", 2),
                        ("second", 2),
                        ("third", 2),
                    ],
                )
                self.assertEqual(
                    [type(exc) for exc in raised.exception.exceptions],
                    [ValueError, RuntimeError, RuntimeError],
                )
                self.assertEqual(
                    [str(exc) for exc in raised.exception.exceptions],
                    ["first/1", "second/1", "second/2"],
                )
                self.assertEqual(raised.exception.accepted_events, tuple(events))
                self.assertEqual(raised.exception.accepted_count, 2)
                self.assertEqual([event.sequence for event in store.events()], [1, 2])
            finally:
                store.close()

    def test_mixed_duplicate_batch_notifies_only_newly_accepted_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                bus = MarketEventBus(store)
                received: list[int] = []
                bus.subscribe(lambda event: received.append(event.sequence))
                first = self._event(1)
                second = self._event(2)

                self.assertTrue(bus.publish(first))
                self.assertEqual(received, [1])

                accepted = bus.publish_many([first, second])

                self.assertEqual(accepted, 1)
                self.assertEqual(received, [1, 2])
                self.assertEqual([event.sequence for event in store.events()], [1, 2])
            finally:
                store.close()

    def test_mixed_duplicate_delivery_failure_carries_exact_accepted_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                bus = MarketEventBus(store)
                first = self._event(1)
                second = self._event(2)
                self.assertTrue(bus.publish(first))

                attempted: list[int] = []
                received: list[int] = []

                def fail(event: MarketEvent) -> None:
                    attempted.append(event.sequence)
                    raise RuntimeError("consumer failed")

                bus.subscribe(fail)
                bus.subscribe(lambda event: received.append(event.sequence))

                with self.assertRaises(MarketEventDeliveryError) as raised:
                    bus.publish_many([first, second])

                self.assertEqual(raised.exception.accepted_events, (second,))
                self.assertEqual(raised.exception.accepted_count, 1)
                self.assertEqual(attempted, [2])
                self.assertEqual(received, [2])
                self.assertEqual([event.sequence for event in store.events()], [1, 2])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
