import unittest

from autosport.parlayapi_provider import HttpJsonResponse, ParlayApiTableTennisProvider


class ParlayApiBoundedSnapshotDrainTests(unittest.TestCase):
    @staticmethod
    def _event(event_id: str, selections: list[str]) -> dict:
        return {
            "id": event_id,
            "bookmakers": [
                {
                    "key": "book-a",
                    "last_update": "2026-09-14T08:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "last_update": "2026-09-14T08:00:00Z",
                            "outcomes": [
                                {"name": selection, "price": 2.0}
                                for selection in selections
                            ],
                        }
                    ],
                }
            ],
        }

    def test_bounded_reads_drain_one_fetched_snapshot_before_refetch(self):
        calls: list[str] = []
        payloads = iter(
            [
                [self._event("event-1", ["A", "B", "C", "D", "E"])],
                [self._event("event-2", ["F"])],
            ]
        )
        clock_values = iter(
            [
                "2026-09-14T08:00:10+00:00",
                "2026-09-14T08:00:20+00:00",
            ]
        )

        def transport(url, headers, timeout):
            calls.append(url)
            return HttpJsonResponse(next(payloads), 200, {})

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            clock=lambda: next(clock_values),
        )

        first = provider.read_batch(max_items=2)
        second = provider.read_batch(max_items=2)
        third = provider.read_batch(max_items=2)

        self.assertEqual(len(calls), 1)
        self.assertEqual([len(first.quotes), len(second.quotes), len(third.quotes)], [2, 2, 1])
        self.assertEqual(first.quality_flags, ("TRUNCATED_BATCH",))
        self.assertEqual(second.quality_flags, ("TRUNCATED_BATCH",))
        self.assertEqual(third.quality_flags, ())
        self.assertEqual(
            [quote.provider_selection_id for batch in (first, second, third) for quote in batch.quotes],
            ["A", "B", "C", "D", "E"],
        )
        self.assertEqual(
            {first.cursor, second.cursor, third.cursor},
            {"2026-09-14T08:00:10+00:00"},
        )

        next_snapshot = provider.read_batch(max_items=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual([quote.provider_event_id for quote in next_snapshot.quotes], ["event-2"])
        self.assertEqual(next_snapshot.cursor, "2026-09-14T08:00:20+00:00")
        self.assertEqual(next_snapshot.quality_flags, ())


if __name__ == "__main__":
    unittest.main()
