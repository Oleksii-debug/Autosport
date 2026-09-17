import tempfile
import threading
import unittest
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore


class SourceHealthStoreConcurrencyTests(unittest.TestCase):
    @staticmethod
    def _record_success(
        store: SourceHealthStore,
        *,
        cursor: str,
        now: str = "2026-09-14T00:00:00+00:00",
    ) -> None:
        store.record_success(
            "source",
            now=now,
            received=1,
            accepted=1,
            rejected=0,
            cursor=cursor,
            latest_source_ts="2026-09-13T23:59:59+00:00",
            quality_flags=(),
        )

    def test_writer_guard_serializes_independent_store_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_health.json"
            first = SourceHealthStore(path)
            second = SourceHealthStore(path)
            started = threading.Event()
            finished = threading.Event()
            errors: list[BaseException] = []

            def writer() -> None:
                started.set()
                try:
                    self._record_success(second, cursor="second")
                except BaseException as exc:  # pragma: no cover - surfaced by assertion below
                    errors.append(exc)
                finally:
                    finished.set()

            with first._writer_guard():
                thread = threading.Thread(target=writer, name="source-health-writer")
                thread.start()
                self.assertTrue(started.wait(timeout=2))
                self.assertFalse(finished.wait(timeout=0.1))

            self.assertTrue(finished.wait(timeout=5))
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(first.get("source").poll_count, 1)

    def test_concurrent_equal_time_success_and_failure_updates_do_not_lose_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_health.json"
            success_store = SourceHealthStore(path)
            failure_store = SourceHealthStore(path)
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []
            rounds = 6
            same_now = "2026-09-14T00:00:00+00:00"

            def success_writer() -> None:
                try:
                    for index in range(rounds):
                        barrier.wait(timeout=5)
                        self._record_success(
                            success_store,
                            cursor=f"success-{index}",
                            now=same_now,
                        )
                except BaseException as exc:  # pragma: no cover - surfaced by assertion below
                    errors.append(exc)

            def failure_writer() -> None:
                try:
                    for index in range(rounds):
                        barrier.wait(timeout=5)
                        failure_store.record_failure(
                            "source",
                            now=same_now,
                            error=RuntimeError(f"failure-{index}"),
                        )
                except BaseException as exc:  # pragma: no cover - surfaced by assertion below
                    errors.append(exc)

            threads = [
                threading.Thread(target=success_writer, name="source-health-success"),
                threading.Thread(target=failure_writer, name="source-health-failure"),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            reopened = SourceHealthStore(path)
            state = reopened.get("source")
            self.assertEqual(state.poll_count, rounds * 2)
            self.assertEqual(state.total_received, rounds)
            self.assertEqual(state.total_accepted, rounds)
            self.assertEqual(state.total_rejected, 0)
            self.assertEqual(state.total_failures, rounds)
            self.assertEqual(state.latest_source_ts, "2026-09-13T23:59:59+00:00")

            # Every writer used the same evidence time, but durable transition order
            # must still preserve all twelve causal states without timestamp invention.
            import json

            persisted = json.loads(path.read_text(encoding="utf-8"))
            entries = persisted["history"]["source"]
            self.assertEqual([entry["transition_order"] for entry in entries], list(range(1, 13)))
            self.assertEqual({entry["recorded_at"] for entry in entries}, {same_now})


if __name__ == "__main__":
    unittest.main()
