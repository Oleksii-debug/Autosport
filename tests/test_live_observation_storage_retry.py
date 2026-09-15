import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.ingestion import CommittedIngestionHealthError
from autosport.ingestion_health import SourceHealthStore
from autosport.live_observation import observe_workspace_once
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)
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

    def test_exhausted_storage_failures_refetch_before_tail_progress(self):
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

        def fail_first_two_appends(store, events):
            nonlocal append_attempts
            materialized = tuple(events)
            append_attempts += 1
            if append_attempts <= 2:
                raise sqlite3.OperationalError("injected repeated storage failure")
            return original_append(store, materialized)

        with tempfile.TemporaryDirectory() as tmp:
            market_path = Path(tmp) / "market.db"
            with patch.object(
                SQLiteMarketStore,
                "append_batch_accepted",
                new=fail_first_two_appends,
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    observe_workspace_once(
                        tmp,
                        provider,
                        max_items=2,
                        clock=lambda: "2026-09-14T08:00:10+00:00",
                    )
                recovered = observe_workspace_once(
                    tmp,
                    provider,
                    max_items=2,
                    clock=lambda: "2026-09-14T08:00:10+00:00",
                )

            store = SQLiteMarketStore(market_path)
            try:
                persisted = store.events()
            finally:
                store.close()

        self.assertEqual(len(transport_calls), 2)
        self.assertEqual(append_attempts, 5)
        self.assertEqual(recovered.stats.received, 5)
        self.assertEqual(recovered.stats.accepted, 5)
        self.assertEqual(recovered.stats.rejected, 0)
        self.assertEqual(len(persisted), 5)
        self.assertEqual(
            {event.selection_id for event in persisted},
            {
                "parlayapi:table_tennis:A",
                "parlayapi:table_tennis:B",
                "parlayapi:table_tennis:C",
                "parlayapi:table_tennis:D",
                "parlayapi:table_tennis:E",
            },
        )

    def test_deferred_payload_failure_clears_pending_snapshot_before_next_read(self):
        transport_calls: list[str] = []
        payloads = iter(
            [
                [
                    self._event("event-1", ["A", "B"]),
                    {"id": "broken-event", "bookmakers": "not-a-list"},
                ],
                [self._event("event-2", ["C"])],
            ]
        )
        clock_values = iter(
            [
                "2026-09-14T08:00:10+00:00",
                "2026-09-14T08:00:20+00:00",
            ]
        )

        def transport(url, headers, timeout):
            transport_calls.append(url)
            return HttpJsonResponse(next(payloads), 200, {})

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            clock=lambda: next(clock_values),
        )

        first = provider.read_batch(max_items=1)
        self.assertEqual([quote.provider_selection_id for quote in first.quotes], ["A"])
        self.assertEqual(first.quality_flags, ("TRUNCATED_BATCH",))
        self.assertEqual(len(transport_calls), 1)

        with self.assertRaises(ProviderPayloadError):
            provider.read_batch(max_items=1)
        self.assertEqual(len(transport_calls), 1)

        recovered = provider.read_batch(max_items=1)
        self.assertEqual(len(transport_calls), 2)
        self.assertEqual(
            [quote.provider_event_id for quote in recovered.quotes],
            ["event-2"],
        )
        self.assertEqual(
            [quote.provider_selection_id for quote in recovered.quotes],
            ["C"],
        )
        self.assertEqual(recovered.cursor, "2026-09-14T08:00:20+00:00")

    def test_post_commit_source_health_failure_is_not_replayed(self):
        transport_calls: list[str] = []
        payload = [self._event("event-1", ["A"])]

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
        health_success_attempts = 0

        def track_append(store, events):
            nonlocal append_attempts
            append_attempts += 1
            return original_append(store, tuple(events))

        def fail_health_success(health_store, *args, **kwargs):
            nonlocal health_success_attempts
            health_success_attempts += 1
            raise OSError("injected post-market-commit source-health failure")

        with tempfile.TemporaryDirectory() as tmp:
            market_path = Path(tmp) / "market.db"
            with patch.object(
                SQLiteMarketStore,
                "append_batch_accepted",
                new=track_append,
            ), patch.object(
                SourceHealthStore,
                "record_success",
                new=fail_health_success,
            ):
                with self.assertRaises(CommittedIngestionHealthError) as caught:
                    observe_workspace_once(
                        tmp,
                        provider,
                        max_items=2,
                        clock=lambda: "2026-09-14T08:00:10+00:00",
                    )

            store = SQLiteMarketStore(market_path)
            try:
                persisted = store.events()
            finally:
                store.close()

        error = caught.exception
        self.assertEqual(error.outcome.source_id, "parlayapi:table_tennis")
        self.assertEqual(error.outcome.received, 1)
        self.assertEqual(error.outcome.accepted, 1)
        self.assertEqual(error.outcome.rejected, 0)
        self.assertIsNone(error.delivery_error)
        self.assertEqual(len(transport_calls), 1)
        self.assertEqual(append_attempts, 1)
        self.assertEqual(health_success_attempts, 1)
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].selection_id, "parlayapi:table_tennis:A")


if __name__ == "__main__":
    unittest.main()
