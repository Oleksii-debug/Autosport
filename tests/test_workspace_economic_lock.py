import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autosport.dataset import load_dataset
from autosport.recovery import reconcile_late_crashes
from autosport.run_registry import ReconciliationError, RunRegistry
from autosport.session import AutosportSession
from autosport.workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


def _hold_workspace_lock(workspace: str, ready, release) -> None:
    with WorkspaceEconomicLock(workspace):
        ready.set()
        if not release.wait(20):
            raise RuntimeError("test lock holder timed out waiting for release")


class WorkspaceEconomicLockTests(unittest.TestCase):
    def _start_holder(self, root: Path):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_workspace_lock,
            args=(str(root), ready, release),
        )
        process.start()
        self.assertTrue(ready.wait(20), "child process did not acquire workspace lock")
        return process, release

    def _stop_holder(self, process, release) -> None:
        release.set()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(10)
            self.fail("child lock-holder process did not exit")
        self.assertEqual(process.exitcode, 0)

    def test_second_process_cannot_acquire_same_workspace_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process, release = self._start_holder(root)
            try:
                with self.assertRaisesRegex(
                    WorkspaceEconomicLockError,
                    "another Autosport process owns",
                ):
                    with WorkspaceEconomicLock(root):
                        self.fail("second process unexpectedly acquired economic lock")
            finally:
                self._stop_holder(process, release)

            # The lock file may persist, but an exited owner must release the OS lock.
            with WorkspaceEconomicLock(root):
                pass

    def test_dataset_run_is_rejected_before_registry_mutation_while_other_process_owns_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = AutosportSession(root, "10000")
            process, release = self._start_holder(root)
            try:
                with self.assertRaises(WorkspaceEconomicLockError):
                    session.run_dataset(load_dataset(Path("examples/tt_demo")))
                self.assertEqual(RunRegistry(root / "run_registry.json").in_progress(), ())
            finally:
                self._stop_holder(process, release)
                session.close()

    def test_recovery_fails_closed_while_active_writer_holds_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            RunRegistry(root / "run_registry.json")
            process, release = self._start_holder(root)
            try:
                with self.assertRaisesRegex(
                    ReconciliationError,
                    "active economic writer",
                ):
                    reconcile_late_crashes(root)
            finally:
                self._stop_holder(process, release)

    def test_unlock_failure_does_not_leave_lock_object_logically_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = WorkspaceEconomicLock(root)
            lock.acquire()

            with mock.patch.object(
                WorkspaceEconomicLock,
                "_unlock_handle",
                side_effect=OSError("simulated unlock failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated unlock failure"):
                    lock.release()

            self.assertIsNone(lock._handle)
            # Closing the handle is the final OS-level release fallback. The same
            # object must therefore be reusable rather than falsely reporting that
            # it still owns the previous, already-closed handle.
            lock.acquire()
            lock.release()

    def test_context_manager_preserves_primary_failure_when_release_also_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = WorkspaceEconomicLock(root)

            with mock.patch.object(
                WorkspaceEconomicLock,
                "_unlock_handle",
                side_effect=OSError("simulated unlock failure"),
            ):
                with self.assertRaisesRegex(ValueError, "primary economic failure") as caught:
                    with lock:
                        raise ValueError("primary economic failure")

            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(
                any(
                    "WorkspaceEconomicLock release also failed" in note
                    and "simulated unlock failure" in note
                    for note in notes
                ),
                f"secondary release evidence missing from primary exception notes: {notes!r}",
            )
            self.assertIsNone(lock._handle)

    def test_context_manager_surfaces_release_failure_after_successful_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = WorkspaceEconomicLock(root)

            with mock.patch.object(
                WorkspaceEconomicLock,
                "_unlock_handle",
                side_effect=OSError("simulated unlock failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated unlock failure"):
                    with lock:
                        pass

            self.assertIsNone(lock._handle)
            lock.acquire()
            lock.release()


if __name__ == "__main__":
    unittest.main()
