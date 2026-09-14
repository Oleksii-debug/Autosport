from __future__ import annotations

import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore


def _record_success(store: SourceHealthStore, *, cursor: str) -> None:
    store.record_success(
        "source",
        now="2026-09-14T00:00:00+00:00",
        received=1,
        accepted=1,
        rejected=0,
        cursor=cursor,
        latest_source_ts="2026-09-13T23:59:59+00:00",
        quality_flags=(),
    )


def _hold_writer_lock(path: str, acquired, release) -> None:
    store = SourceHealthStore(Path(path))
    with store._writer_guard():
        acquired.set()
        if not release.wait(timeout=10):
            raise RuntimeError("timed out waiting to release source-health writer lock")


def _write_success(path: str, attempted, finished, cursor: str) -> None:
    attempted.set()
    store = SourceHealthStore(Path(path))
    _record_success(store, cursor=cursor)
    finished.set()


def _hold_writer_lock_then_crash(path: str, acquired) -> None:
    store = SourceHealthStore(Path(path))
    guard = store._writer_guard()
    guard.__enter__()
    acquired.set()
    os._exit(0)


def _success_rounds(path: str, barrier, rounds: int) -> None:
    store = SourceHealthStore(Path(path))
    for index in range(rounds):
        barrier.wait(timeout=10)
        _record_success(store, cursor=f"success-{index}")


def _failure_rounds(path: str, barrier, rounds: int) -> None:
    store = SourceHealthStore(Path(path))
    for index in range(rounds):
        barrier.wait(timeout=10)
        store.record_failure(
            "source",
            now="2026-09-14T00:00:00+00:00",
            error=RuntimeError(f"failure-{index}"),
        )


class SourceHealthMultiprocessLockTests(unittest.TestCase):
    @staticmethod
    def _context():
        # Always exercise spawn semantics so the regression covers Windows rather
        # than accidentally relying on POSIX fork inheritance.
        return multiprocessing.get_context("spawn")

    def test_independent_process_is_blocked_until_writer_lock_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_health.json"
            SourceHealthStore(path)
            ctx = self._context()
            acquired = ctx.Event()
            release = ctx.Event()
            attempted = ctx.Event()
            finished = ctx.Event()

            holder = ctx.Process(
                target=_hold_writer_lock,
                args=(str(path), acquired, release),
                name="source-health-lock-holder",
            )
            writer = ctx.Process(
                target=_write_success,
                args=(str(path), attempted, finished, "after-holder"),
                name="source-health-blocked-writer",
            )
            holder.start()
            self.assertTrue(acquired.wait(timeout=10))
            writer.start()
            self.assertTrue(attempted.wait(timeout=10))
            self.assertFalse(
                finished.wait(timeout=0.25),
                "independent writer completed while another process owned the lock",
            )

            release.set()
            holder.join(timeout=10)
            writer.join(timeout=10)
            self.assertFalse(holder.is_alive())
            self.assertFalse(writer.is_alive())
            self.assertEqual(holder.exitcode, 0)
            self.assertEqual(writer.exitcode, 0)
            self.assertTrue(finished.is_set())
            self.assertEqual(SourceHealthStore(path).get("source").poll_count, 1)

    def test_process_exit_releases_writer_lock_for_fresh_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_health.json"
            SourceHealthStore(path)
            ctx = self._context()
            acquired = ctx.Event()

            crashing_holder = ctx.Process(
                target=_hold_writer_lock_then_crash,
                args=(str(path), acquired),
                name="source-health-crashing-lock-holder",
            )
            crashing_holder.start()
            self.assertTrue(acquired.wait(timeout=10))
            crashing_holder.join(timeout=10)
            self.assertFalse(crashing_holder.is_alive())
            self.assertEqual(crashing_holder.exitcode, 0)

            attempted = ctx.Event()
            finished = ctx.Event()
            writer = ctx.Process(
                target=_write_success,
                args=(str(path), attempted, finished, "after-crash"),
                name="source-health-post-crash-writer",
            )
            writer.start()
            self.assertTrue(attempted.wait(timeout=10))
            writer.join(timeout=10)
            self.assertFalse(writer.is_alive())
            self.assertEqual(writer.exitcode, 0)
            self.assertTrue(finished.is_set())
            self.assertEqual(SourceHealthStore(path).get("source").poll_count, 1)

    def test_two_processes_preserve_exact_cumulative_counters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_health.json"
            SourceHealthStore(path)
            ctx = self._context()
            rounds = 6
            barrier = ctx.Barrier(2)
            success = ctx.Process(
                target=_success_rounds,
                args=(str(path), barrier, rounds),
                name="source-health-success-process",
            )
            failure = ctx.Process(
                target=_failure_rounds,
                args=(str(path), barrier, rounds),
                name="source-health-failure-process",
            )

            success.start()
            failure.start()
            success.join(timeout=30)
            failure.join(timeout=30)
            self.assertFalse(success.is_alive())
            self.assertFalse(failure.is_alive())
            self.assertEqual(success.exitcode, 0)
            self.assertEqual(failure.exitcode, 0)

            state = SourceHealthStore(path).get("source")
            self.assertEqual(state.poll_count, rounds * 2)
            self.assertEqual(state.total_received, rounds)
            self.assertEqual(state.total_accepted, rounds)
            self.assertEqual(state.total_rejected, 0)
            self.assertEqual(state.total_failures, rounds)


if __name__ == "__main__":
    unittest.main()
