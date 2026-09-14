import io
import multiprocessing
import os
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

    def test_symlink_lock_path_is_rejected_without_touching_external_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "workspace"
            root.mkdir()
            external = base / "external-lock-target.bin"
            external.write_bytes(b"")
            lock_path = root / WorkspaceEconomicLock.FILE_NAME
            try:
                os.symlink(external, lock_path)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation is unavailable on this platform: {exc}")

            lock = WorkspaceEconomicLock(root)
            with self.assertRaisesRegex(
                WorkspaceEconomicLockError,
                "regular non-symlink file",
            ):
                lock.acquire()

            self.assertEqual(external.read_bytes(), b"")
            self.assertTrue(lock_path.is_symlink())
            self.assertIsNone(lock._handle)

    def test_hardlinked_lock_path_is_rejected_without_touching_alias_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "workspace"
            root.mkdir()
            external = base / "external-lock-target.bin"
            external.write_bytes(b"external-state")
            lock_path = root / WorkspaceEconomicLock.FILE_NAME
            try:
                os.link(external, lock_path)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"hard-link creation is unavailable on this platform: {exc}")

            lock = WorkspaceEconomicLock(root)
            with self.assertRaisesRegex(
                WorkspaceEconomicLockError,
                "hard-link aliases",
            ):
                lock.acquire()

            self.assertEqual(external.read_bytes(), b"external-state")
            self.assertEqual(lock_path.read_bytes(), b"external-state")
            self.assertIsNone(lock._handle)

    @unittest.skipIf(os.name == "nt", "Windows normally forbids replacing an open lock pathname")
    def test_path_swap_after_open_is_rejected_before_acquisition_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / WorkspaceEconomicLock.FILE_NAME
            lock_path.write_bytes(b"\0")
            lock = WorkspaceEconomicLock(root)
            original_open = Path.open

            def open_then_replace(path_obj, *args, **kwargs):
                handle = original_open(path_obj, *args, **kwargs)
                os.unlink(path_obj)
                with io.open(path_obj, "wb") as replacement:
                    replacement.write(b"replacement")
                return handle

            with mock.patch.object(Path, "open", autospec=True, side_effect=open_then_replace):
                with self.assertRaisesRegex(
                    WorkspaceEconomicLockError,
                    "changed during acquisition",
                ):
                    lock.acquire()

            self.assertEqual(lock_path.read_bytes(), b"replacement")
            self.assertIsNone(lock._handle)

    @unittest.skipIf(os.name == "nt", "Windows normally forbids replacing an open lock pathname")
    def test_path_swap_after_os_lock_is_rejected_before_acquisition_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / WorkspaceEconomicLock.FILE_NAME
            lock = WorkspaceEconomicLock(root)
            original_lock = WorkspaceEconomicLock._lock_handle

            def lock_then_replace(handle):
                original_lock(handle)
                os.unlink(lock_path)
                lock_path.write_bytes(b"replacement-after-lock")

            with mock.patch.object(
                WorkspaceEconomicLock,
                "_lock_handle",
                side_effect=lock_then_replace,
            ):
                with self.assertRaisesRegex(
                    WorkspaceEconomicLockError,
                    "changed during acquisition",
                ):
                    lock.acquire()

            self.assertEqual(lock_path.read_bytes(), b"replacement-after-lock")
            self.assertIsNone(lock._handle)

            # Cleanup closed the old locked inode, so the replacement canonical path
            # can be acquired normally rather than leaving hidden ownership behind.
            with WorkspaceEconomicLock(root):
                pass

    def test_cooperating_reacquire_preserves_canonical_lock_file_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = WorkspaceEconomicLock(root)
            first.acquire()
            lock_path = first.path
            first_path_stat = os.stat(lock_path, follow_symlinks=False)
            first_handle_stat = os.fstat(first._handle.fileno())
            self.assertTrue(os.path.samestat(first_path_stat, first_handle_stat))
            first.release()

            after_release_stat = os.stat(lock_path, follow_symlinks=False)
            self.assertTrue(os.path.samestat(first_path_stat, after_release_stat))

            second = WorkspaceEconomicLock(root)
            second.acquire()
            try:
                second_path_stat = os.stat(lock_path, follow_symlinks=False)
                second_handle_stat = os.fstat(second._handle.fileno())
                self.assertTrue(os.path.samestat(first_path_stat, second_path_stat))
                self.assertTrue(os.path.samestat(second_path_stat, second_handle_stat))
            finally:
                second.release()

    @unittest.skipIf(os.name == "nt", "Windows normally forbids replacing an open lock pathname")
    def test_external_replacement_after_final_checkpoint_is_outside_advisory_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = WorkspaceEconomicLock(root)
            lock_path = first.path
            original_validate = first._validate_open_handle_identity
            validation_count = 0

            def validate_then_replace_after_final(handle):
                nonlocal validation_count
                validation_count += 1
                original_validate(handle)
                if validation_count == 2:
                    # Deliberately act *after* the final validated checkpoint. POSIX
                    # advisory file locking cannot make a pathname immutable here;
                    # this external filesystem mutation is explicitly outside the
                    # cooperating-Autosport contract and this regression prevents a
                    # future implementation/report from overclaiming otherwise.
                    os.unlink(lock_path)
                    lock_path.write_bytes(b"externally-replaced-after-checkpoint")

            with mock.patch.object(
                first,
                "_validate_open_handle_identity",
                side_effect=validate_then_replace_after_final,
            ):
                first.acquire()

            self.assertIsNotNone(first._handle)
            self.assertEqual(lock_path.read_bytes(), b"externally-replaced-after-checkpoint")

            # The replacement inode is independently lockable. This is the explicit
            # threat-model boundary, not a compliant-writer behavior.
            second = WorkspaceEconomicLock(root)
            second.acquire()
            try:
                self.assertIsNotNone(second._handle)
                self.assertFalse(
                    os.path.samestat(
                        os.fstat(first._handle.fileno()),
                        os.fstat(second._handle.fileno()),
                    )
                )
            finally:
                second.release()
                first.release()

    def test_acquire_preserves_primary_failure_when_cleanup_close_also_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = WorkspaceEconomicLock(root)
            handle = mock.MagicMock()
            handle.tell.return_value = 1
            handle.close.side_effect = OSError("simulated close failure")
            primary = WorkspaceEconomicLockError(
                "another Autosport process owns the workspace economic-writer lock"
            )

            with (
                mock.patch.object(lock, "_open_new_lock_handle", return_value=handle),
                mock.patch.object(lock, "_validate_open_handle_identity"),
                mock.patch.object(WorkspaceEconomicLock, "_lock_handle", side_effect=primary),
            ):
                with self.assertRaisesRegex(
                    WorkspaceEconomicLockError,
                    "another Autosport process owns",
                ) as caught:
                    lock.acquire()

            self.assertIs(caught.exception, primary)
            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(
                any(
                    "cleaning up acquisition failure" in note
                    and "simulated close failure" in note
                    for note in notes
                ),
                f"secondary acquisition-cleanup evidence missing from primary exception notes: {notes!r}",
            )
            self.assertIs(lock._handle, handle)
            with self.assertRaisesRegex(WorkspaceEconomicLockError, "already held"):
                lock.acquire()

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

    def test_dual_unlock_and_close_failure_keeps_lock_object_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = WorkspaceEconomicLock(root)
            handle = mock.MagicMock()
            handle.close.side_effect = OSError("simulated close failure")
            lock._handle = handle

            with mock.patch.object(
                WorkspaceEconomicLock,
                "_unlock_handle",
                side_effect=OSError("simulated unlock failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated unlock failure") as caught:
                    lock.release()

            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(
                any(
                    "handle close also failed after unlock failure" in note
                    and "simulated close failure" in note
                    for note in notes
                ),
                f"secondary close evidence missing from unlock exception notes: {notes!r}",
            )
            self.assertIs(lock._handle, handle)
            with self.assertRaisesRegex(WorkspaceEconomicLockError, "already held"):
                lock.acquire()

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
