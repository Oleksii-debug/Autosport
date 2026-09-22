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

            barrier = threading.Barrier(2)
            errors: list[BaseException] = []

            def synchronize_first_read(state: _CollectorServiceState) -> None:
                original_read = state._read
                first_read = True

                def synchronized_read() -> dict[str, object]:
                    nonlocal first_read
                    snapshot = original_read()
                    if first_read:
                        first_read = False
                        barrier.wait(timeout=5)
                    return snapshot

                state._read = synchronized_read  # type: ignore[method-assign]

            synchronize_first_read(first)
            synchronize_first_read(second)

            def mutate(state: _CollectorServiceState) -> None:
                try:
                    state.record_provider_failure(code="PROVIDER_UNAVAILABLE")
                except BaseException as exc:  # pragma: no cover - diagnostic capture
                    errors.append(exc)

            threads = (
                threading.Thread(target=mutate, args=(first,)),
                threading.Thread(target=mutate, args=(second,)),
            )
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])

            snapshot = first.snapshot()
            self.assertEqual(
                snapshot["provider_failures"],
                2,
                "concurrent durable collector-state updates must not lose one mutation",
            )


if __name__ == "__main__":
    unittest.main()
