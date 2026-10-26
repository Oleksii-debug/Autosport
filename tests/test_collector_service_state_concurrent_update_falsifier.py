from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from autosport.collector_service import _CollectorServiceState


class CollectorServiceStateConcurrentUpdateFalsifierTests(unittest.TestCase):
    STARTED_AT = "2026-09-22T03:00:00+00:00"

    def _state_pair(
        self,
        path: Path,
    ) -> tuple[_CollectorServiceState, _CollectorServiceState]:
        first = _CollectorServiceState(
            path,
            run_id="run-shared",
            source_id="source-shared",
            started_at=self.STARTED_AT,
        )
        second = _CollectorServiceState(
            path,
            run_id="run-shared",
            source_id="source-shared",
            started_at=self.STARTED_AT,
        )
        return first, second

    def test_sequential_independent_instances_preserve_both_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector_state.json"
            first, second = self._state_pair(path)

            first.record_provider_failure(code="PROVIDER_UNAVAILABLE")
            second.record_provider_failure(code="PROVIDER_UNAVAILABLE")

            snapshot = first.snapshot()
            self.assertEqual(snapshot["provider_failures"], 2)

    def test_concurrent_independent_instances_cannot_lose_durable_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector_state.json"
            first, second = self._state_pair(path)

            first_snapshot_read = threading.Event()
            second_snapshot_read = threading.Event()
            errors: list[BaseException] = []

            first_original_read = first._read
            first_read_pending = True

            def first_synchronized_read() -> dict[str, object]:
                nonlocal first_read_pending
                snapshot = first_original_read()
                if first_read_pending:
                    first_read_pending = False
                    first_snapshot_read.set()
                    second_snapshot_read.wait(timeout=2)
                return snapshot

            second_original_read = second._read
            second_read_pending = True

            def second_synchronized_read() -> dict[str, object]:
                nonlocal second_read_pending
                snapshot = second_original_read()
                if second_read_pending:
                    second_read_pending = False
                    second_snapshot_read.set()
                return snapshot

            first._read = first_synchronized_read  # type: ignore[method-assign]
            second._read = second_synchronized_read  # type: ignore[method-assign]

            def mutate(state: _CollectorServiceState) -> None:
                try:
                    state.record_provider_failure(code="PROVIDER_UNAVAILABLE")
                except BaseException as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)

            first_thread = threading.Thread(target=mutate, args=(first,))
            second_thread = threading.Thread(target=mutate, args=(second,))

            first_thread.start()
            self.assertTrue(
                first_snapshot_read.wait(timeout=5),
                "first mutation never reached its durable predecessor read",
            )
            second_thread.start()

            first_thread.join(timeout=10)
            second_thread.join(timeout=10)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(errors, [])

            snapshot = first.snapshot()
            self.assertEqual(
                snapshot["provider_failures"],
                2,
                "concurrent durable collector-state updates must not lose one mutation",
            )


if __name__ == "__main__":
    unittest.main()
