import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from autosport.live_observation import observe_workspace_once
from autosport.parlayapi_provider import HttpJsonResponse, ParlayApiTableTennisProvider
from autosport.storage import SQLiteMarketStore


class LiveObservationStorageRetryTests(unittest.TestCase):
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

    def test_transient_storage_failure_replays_same_chunk_before_tail_progress(self):
        transport_calls: list[str] = []
        payload = [self._event("event-1", ["A", "B", "C", "D", "E"])]

        def transport(url, headers, timeout):
            transport_calls.append(url)
            return HttpJsonResponse(payload, 200, {})

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            clock=lambda: "2026-09-14T08:00:10+00:00",
        )

        original_append = SQLiteMarketStore.append_batch_accepted
        append_attempts = 0
        failed_chunk: tuple[str, ...] | None = None
        persisted_chunks: list[tuple[str, ...]] = []

        def fail_first_append(store, events):
            nonlocal append_attempts, failed_chunk
            materialized = tuple(events)
            selection_ids = tuple(event.selection_id for event in materialized)
            append_attempts += 1
            if append_attempts == 1:
                failed_chunk = selection_ids
                raise sqlite3.OperationalError("injected transient storage failure")
            persisted_chunks.append(selection_ids)
            return original_append(store, materialized)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                SQLiteMarketStore,
                "append_batch_accepted",
                new=fail_first_append,
            ):
                result = observe_workspace_once(
                    tmp,
                    provider,
                    max_items=2,
                    clock=lambda: "2026-09-14T08:00:10+00:00",
                )

        self.assertEqual(len(transport_calls), 1)
        self.assertEqual(append_attempts, 4)
        self.assertIsNotNone(failed_chunk)
        self.assertEqual(failed_chunk, persisted_chunks[0])
        self.assertEqual([len(chunk) for chunk in persisted_chunks], [2, 2, 1])
        self.assertEqual(result.stats.received, 5)
        self.assertEqual(result.stats.accepted, 5)
        self.assertEqual(result.stats.rejected, 0)
        self.assertEqual(len(result.current_quotes), 5)
        self.assertEqual(
            {event.selection_id for event in result.current_quotes},
            {
                "parlayapi:table_tennis:A",
                "parlayapi:table_tennis:B",
                "parlayapi:table_tennis:C",
                "parlayapi:table_tennis:D",
                "parlayapi:table_tennis:E",
            },
        )


if __name__ == "__main__":
    unittest.main()
