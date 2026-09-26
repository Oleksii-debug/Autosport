from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from autosport.collector_service import (
    CollectorServiceStoppedError,
    _CollectorServiceState,
)


class CollectorServiceStateStopSerializationTests(unittest.TestCase):
    STARTED_AT = "2026-09-25T17:00:00+00:00"
    STOPPED_AT = "2026-09-25T17:00:10+00:00"

    def _state_pair(
        self,
        path: Path,
    ) -> tuple[_CollectorServiceState, _CollectorServiceState]:
        first = _CollectorServiceState(
            path,
            run_id="run-stop-race",
            source_id="source-stop-race",
            started_at=self.STARTED_AT,
        )
        second = _CollectorServiceState(
            path,
            run_id="run-stop-race",
            source_id="source-stop-race",
            started_at=self.STARTED_AT,
        )
        return first, second

    def test_inflight_failure_cannot_overwrite_durable_stop(self) -> None:
        """A mutation based on pre-STOP bytes must not publish after STOP."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector_state.json"
            updater, stopper = self._state_pair(path)

            updater_snapshot_read = threading.Event()
            stop_finished = threading.Event()
            errors: list[BaseException] = []

            original_read = updater._read
            first_read_pending = True

            def synchronized_read() -> dict[str, object]:
                nonlocal first_read_pending
                snapshot = original_read()
                if first_read_pending:
                    first_read_pending = False
                    updater_snapshot_read.set()
                    # On the historical unlocked implementation STOP can finish here
                    # and the stale updater subsequently erases it. With serialization,
                    # STOP waits on the mutation lock and completes immediately after
                    # this updater releases it.
                    stop_finished.wait(timeout=2)
                return snapshot

            updater._read = synchronized_read  # type: ignore[method-assign]

            def write_failure() -> None:
                try:
                    updater.record_provider_failure(code="PROVIDER_UNAVAILABLE")
                except BaseException as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)

            def write_stop() -> None:
                try:
                    self.assertTrue(updater_snapshot_read.wait(timeout=5))
                    stopper.stop(at=self.STOPPED_AT, reason="operator_stop")
                except BaseException as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)
                finally:
                    stop_finished.set()

            updater_thread = threading.Thread(target=write_failure)
            stop_thread = threading.Thread(target=write_stop)
            updater_thread.start()
            stop_thread.start()
            updater_thread.join(timeout=10)
            stop_thread.join(timeout=10)

            self.assertFalse(updater_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertEqual(errors, [])

            snapshot = stopper.snapshot()
            self.assertEqual(snapshot["provider_failures"], 1)
            self.assertEqual(snapshot["last_error_code"], "PROVIDER_UNAVAILABLE")
            self.assertEqual(snapshot["stopped_at"], self.STOPPED_AT)
            self.assertEqual(snapshot["stop_reason"], "operator_stop")

    def test_durable_stop_rejects_terminal_mutations_until_explicit_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector_state.json"
            state, observer = self._state_pair(path)
            state.stop(at=self.STOPPED_AT, reason="operator_stop")
            stopped = observer.snapshot()

            with self.assertRaises(CollectorServiceStoppedError):
                state.record_success(
                    at="2026-09-25T17:00:11+00:00",
                    committed=1,
                    duplicates=0,
                )
            with self.assertRaises(CollectorServiceStoppedError):
                state.record_provider_failure(code="PROVIDER_UNAVAILABLE")
            with self.assertRaises(CollectorServiceStoppedError):
                state.record_local_failure(code="LOCAL_FAILURE")

            self.assertEqual(observer.snapshot(), stopped)

            state.resume(at="2026-09-25T17:00:12+00:00")
            state.record_success(
                at="2026-09-25T17:00:13+00:00",
                committed=1,
                duplicates=0,
            )
            resumed = observer.snapshot()
            self.assertIsNone(resumed["stopped_at"])
            self.assertIsNone(resumed["stop_reason"])
            self.assertEqual(resumed["cycles_succeeded"], 1)
            self.assertEqual(resumed["deltas_committed"], 1)
            self.assertEqual(
                resumed["last_success_at"],
                "2026-09-25T17:00:13+00:00",
            )


if __name__ == "__main__":
    unittest.main()
