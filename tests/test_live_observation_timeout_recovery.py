import tempfile
import threading
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore
from autosport.live_observation import OneShotObservationWorker, observe_workspace_once
from autosport.providers import ProviderBatch, ProviderQuote
from autosport.storage import SQLiteMarketStore


_SOURCE_ID = "timeout-source"
_T1 = "2026-09-21T18:00:01+00:00"
_T2 = "2026-09-21T18:00:02+00:00"
_T3 = "2026-09-21T18:00:03+00:00"


class _TimeoutProvider:
    def __init__(self, source_id: str = _SOURCE_ID) -> None:
        self.source_id = source_id
        self.calls = 0

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        self.calls += 1
        raise TimeoutError("provider read timed out")


class _FixedBatchProvider:
    def __init__(self, batch: ProviderBatch) -> None:
        self.source_id = batch.source_id
        self._batch = batch
        self.calls = 0

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        self.calls += 1
        return self._batch


def _batch(
    *,
    source_id: str = _SOURCE_ID,
    sequence: int = 1,
    selection: str = "runner-a",
    cursor: str = "cursor-1",
    observed_ts: str = "2026-09-21T18:00:00+00:00",
) -> ProviderBatch:
    return ProviderBatch(
        source_id,
        (
            ProviderQuote(
                provider_event_id="event-1",
                provider_market_id="winner",
                provider_selection_id=selection,
                decimal_odds=Decimal("2.00"),
                observed_ts=observed_ts,
                sequence=sequence,
                source_ts=observed_ts,
            ),
        ),
        cursor=cursor,
    )


class ProviderTimeoutNegativeEvidenceTests(unittest.TestCase):
    def test_timeout_before_first_commit_persists_failure_without_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _TimeoutProvider()

            with self.assertRaisesRegex(TimeoutError, "provider read timed out"):
                observe_workspace_once(
                    root,
                    provider,
                    max_items=10,
                    clock=lambda: _T1,
                )

            self.assertEqual(provider.calls, 1)
            health = SourceHealthStore(root / "source_health.json").get(_SOURCE_ID)
            self.assertEqual(health.status, "failed")
            self.assertEqual(health.poll_count, 1)
            self.assertEqual(health.total_failures, 1)
            self.assertEqual(health.consecutive_failures, 1)
            self.assertIsNone(health.last_success_at)
            self.assertEqual(health.last_error_at, _T1)
            self.assertIn("TimeoutError", health.last_error or "")
            self.assertIsNone(health.last_cursor)
            self.assertIsNone(health.latest_source_ts)

            store = SQLiteMarketStore(root / "market.db")
            try:
                self.assertEqual(store.events(), [])
            finally:
                store.close()

    def test_timeout_after_prior_commit_preserves_durable_market_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = observe_workspace_once(
                root,
                _FixedBatchProvider(_batch()),
                max_items=10,
                clock=lambda: _T1,
            )
            self.assertEqual(first.stats.accepted, 1)
            self.assertEqual(first.health.status, "healthy")

            with self.assertRaisesRegex(TimeoutError, "provider read timed out"):
                observe_workspace_once(
                    root,
                    _TimeoutProvider(),
                    max_items=10,
                    clock=lambda: _T2,
                )

            health = SourceHealthStore(root / "source_health.json").get(_SOURCE_ID)
            self.assertEqual(health.status, "failed")
            self.assertEqual(health.poll_count, 2)
            self.assertEqual(health.total_received, 1)
            self.assertEqual(health.total_accepted, 1)
            self.assertEqual(health.total_rejected, 0)
            self.assertEqual(health.total_failures, 1)
            self.assertEqual(health.last_success_at, _T1)
            self.assertEqual(health.last_error_at, _T2)
            self.assertEqual(health.last_cursor, "cursor-1")
            self.assertEqual(health.latest_source_ts, "2026-09-21T18:00:00+00:00")

            store = SQLiteMarketStore(root / "market.db")
            try:
                persisted = store.events()
                self.assertEqual(len(persisted), 1)
                self.assertEqual(persisted[0].selection_id, f"{_SOURCE_ID}:runner-a")
            finally:
                store.close()

    def test_restart_and_recovery_keep_timeout_in_as_of_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observe_workspace_once(
                root,
                _FixedBatchProvider(_batch()),
                max_items=10,
                clock=lambda: _T1,
            )
            with self.assertRaises(TimeoutError):
                observe_workspace_once(
                    root,
                    _TimeoutProvider(),
                    max_items=10,
                    clock=lambda: _T2,
                )
            observe_workspace_once(
                root,
                _FixedBatchProvider(
                    _batch(
                        sequence=2,
                        selection="runner-b",
                        cursor="cursor-2",
                        observed_ts="2026-09-21T18:00:02+00:00",
                    )
                ),
                max_items=10,
                clock=lambda: _T3,
            )

            reopened = SourceHealthStore(root / "source_health.json")
            current = reopened.get(_SOURCE_ID)
            self.assertEqual(current.status, "healthy")
            self.assertEqual(current.poll_count, 3)
            self.assertEqual(current.total_failures, 1)
            self.assertEqual(current.consecutive_failures, 0)
            self.assertEqual(current.last_success_at, _T3)
            self.assertEqual(current.last_error_at, _T2)
            self.assertIsNone(current.last_error)
            self.assertEqual(current.last_cursor, "cursor-2")

            before_timeout = reopened.get_as_of(
                _SOURCE_ID,
                as_of=datetime.fromisoformat(_T1),
            )
            during_timeout = reopened.get_as_of(
                _SOURCE_ID,
                as_of=datetime.fromisoformat(_T2),
            )
            after_recovery = reopened.get_as_of(
                _SOURCE_ID,
                as_of=datetime.fromisoformat(_T3),
            )
            self.assertEqual(before_timeout.status, "healthy")
            self.assertEqual(before_timeout.total_failures, 0)
            self.assertEqual(during_timeout.status, "failed")
            self.assertEqual(during_timeout.total_failures, 1)
            self.assertEqual(during_timeout.last_cursor, "cursor-1")
            self.assertEqual(after_recovery.status, "healthy")
            self.assertEqual(after_recovery.total_failures, 1)

    def test_alternate_source_success_cannot_launder_failed_source_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(TimeoutError):
                observe_workspace_once(
                    root,
                    _TimeoutProvider("provider-a"),
                    max_items=10,
                    clock=lambda: _T1,
                )

            observe_workspace_once(
                root,
                _FixedBatchProvider(
                    _batch(
                        source_id="provider-b",
                        selection="runner-b",
                        observed_ts="2026-09-21T18:00:01+00:00",
                    )
                ),
                max_items=10,
                clock=lambda: _T2,
            )

            reopened = SourceHealthStore(root / "source_health.json")
            failed = reopened.get("provider-a")
            healthy = reopened.get("provider-b")
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.total_failures, 1)
            self.assertIsNone(failed.last_success_at)
            self.assertEqual(healthy.status, "healthy")
            self.assertEqual(healthy.total_failures, 0)
            self.assertEqual(healthy.last_cursor, "cursor-1")

    def test_worker_timeout_is_terminal_and_releases_single_flight_without_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            expected = observe_workspace_once(
                tmp,
                _FixedBatchProvider(_batch()),
                max_items=10,
                clock=lambda: _T1,
            )

        worker = OneShotObservationWorker()
        entered = threading.Event()

        def timeout_task():
            entered.set()
            raise TimeoutError("provider read timed out")

        self.assertTrue(worker.start(timeout_task))
        self.assertTrue(entered.wait(timeout=1))
        self.assertIsNotNone(worker._thread)
        worker._thread.join(timeout=1)
        self.assertFalse(worker._thread.is_alive())

        failed = worker.poll()
        self.assertIsNotNone(failed)
        self.assertIsNone(failed.result)
        self.assertEqual(failed.error, "TimeoutError: provider read timed out")
        self.assertFalse(worker.busy)

        self.assertTrue(worker.start(lambda: expected))
        self.assertIsNotNone(worker._thread)
        worker._thread.join(timeout=1)
        completed = worker.poll()
        self.assertIsNotNone(completed)
        self.assertIs(completed.result, expected)
        self.assertIsNone(completed.error)
        self.assertFalse(worker.busy)


if __name__ == "__main__":
    unittest.main()
