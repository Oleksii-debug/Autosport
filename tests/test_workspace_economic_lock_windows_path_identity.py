import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from autosport.workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


class WorkspaceEconomicLockWindowsPathIdentityTests(unittest.TestCase):
    @staticmethod
    def _incomplete_path_stat(value):
        return SimpleNamespace(
            st_mode=value.st_mode,
            st_ino=value.st_ino,
            st_dev=0,
            st_nlink=value.st_nlink,
            st_size=value.st_size,
            st_mtime_ns=value.st_mtime_ns,
            st_ctime_ns=value.st_ctime_ns,
        )

    def test_incomplete_path_identity_does_not_reject_unchanged_lock_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / WorkspaceEconomicLock.FILE_NAME
            lock_path.write_bytes(b"\0")
            original_stat = os.stat

            def incomplete_stat(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                try:
                    same_path = Path(path) == lock_path
                except TypeError:
                    same_path = False
                if same_path:
                    return self._incomplete_path_stat(result)
                return result

            with mock.patch("autosport.workspace_lock.os.stat", side_effect=incomplete_stat):
                first = WorkspaceEconomicLock(root)
                first.acquire()
                first.release()
                second = WorkspaceEconomicLock(root)
                second.acquire()
                second.release()

            self.assertEqual(lock_path.read_bytes(), b"\0")

    def test_redirected_verification_handle_is_rejected_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / WorkspaceEconomicLock.FILE_NAME
            lock_path.write_bytes(b"\0")
            replacement = root / "replacement-lock.bin"
            replacement.write_bytes(b"\0")
            original_open = Path.open

            def redirect_verification(path_obj, mode="r", *args, **kwargs):
                if Path(path_obj) == lock_path and mode == "rb":
                    return original_open(replacement, mode, *args, **kwargs)
                return original_open(path_obj, mode, *args, **kwargs)

            lock = WorkspaceEconomicLock(root)
            with mock.patch.object(
                Path,
                "open",
                autospec=True,
                side_effect=redirect_verification,
            ):
                with self.assertRaisesRegex(
                    WorkspaceEconomicLockError,
                    "changed during acquisition",
                ):
                    lock.acquire()

            self.assertIsNone(lock._handle)
            self.assertEqual(lock_path.read_bytes(), b"\0")
            self.assertEqual(replacement.read_bytes(), b"\0")


if __name__ == "__main__":
    unittest.main()
