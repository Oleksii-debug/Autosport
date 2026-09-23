import threading
import unittest
from unittest.mock import patch

import autosport.settlement as settlement_module
from autosport.settlement import SettlementEngine


class _ObservedSerializationLock:
    """Expose acquisition ordering while retaining real mutual exclusion."""

    def __init__(self) -> None:
        self._inner = threading.Lock()
        self._count_lock = threading.Lock()
        self._attempts = 0
        self.first_acquired = threading.Event()
        self.second_attempted = threading.Event()
        self.release_first = threading.Event()

    def __enter__(self):
        with self._count_lock:
            self._attempts += 1
            attempt = self._attempts
        if attempt == 2:
            self.second_attempted.set()
        self._inner.acquire()
        if attempt == 1:
            self.first_acquired.set()
            if not self.release_first.wait(5):
                self._inner.release()
                raise AssertionError(
                    "timed out waiting to release first settlement record"
                )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._inner.release()


class SettlementRecordConcurrencyTests(unittest.TestCase):
    def test_conflicting_concurrent_records_cannot_both_commit(self) -> None:
        engine = SettlementEngine()
        observed_lock = _ObservedSerializationLock()
        results: dict[str, Exception | None] = {}
        results_lock = threading.Lock()

        def record(name: str, outcome: str) -> None:
            error: Exception | None = None
            try:
                engine.record({"event-1|winner|alice": outcome})
            except Exception as exc:
                error = exc
            with results_lock:
                results[name] = error

        with patch.object(
            settlement_module,
            "_SETTLEMENT_OUTCOME_LOCK",
            observed_lock,
        ):
            first = threading.Thread(target=record, args=("first", "win"))
            first.start()
            self.assertTrue(observed_lock.first_acquired.wait(5))

            second = threading.Thread(target=record, args=("second", "loss"))
            second.start()
            self.assertTrue(observed_lock.second_attempted.wait(5))

            observed_lock.release_first.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertIsNone(results["first"])
        self.assertIsInstance(results["second"], ValueError)
        self.assertEqual(
            str(results["second"]),
            "conflicting settlement for event-1|winner|alice",
        )
        self.assertEqual(engine.outcomes, {"event-1|winner|alice": "win"})

    def test_same_outcome_concurrent_replay_remains_idempotent(self) -> None:
        engine = SettlementEngine()
        start = threading.Barrier(3)
        errors: list[Exception] = []
        errors_lock = threading.Lock()

        def record() -> None:
            start.wait()
            try:
                engine.record({"event-1|winner|alice": "void"})
            except Exception as exc:
                with errors_lock:
                    errors.append(exc)

        workers = [threading.Thread(target=record) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(5)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(engine.outcomes, {"event-1|winner|alice": "void"})


if __name__ == "__main__":
    unittest.main()
