import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autosport.workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


class WorkspaceEconomicLockCreationRaceTests(unittest.TestCase):
    def test_race_created_symlink_to_missing_target_cannot_create_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "workspace"
            root.mkdir()
            external = base / "external-race-target.bin"
            lock_path = root / WorkspaceEconomicLock.FILE_NAME

            probe = base / "symlink-probe"
            try:
                os.symlink(external, probe)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation is unavailable on this platform: {exc}")
            else:
                probe.unlink()

            lock = WorkspaceEconomicLock(root)
            original_open_new = lock._open_new_lock_handle
            inserted = False

            def open_new_after_race():
                nonlocal inserted
                if not inserted:
                    inserted = True
                    os.symlink(external, lock_path)
                return original_open_new()

            with mock.patch.object(
                lock,
                "_open_new_lock_handle",
                side_effect=open_new_after_race,
            ):
                with self.assertRaisesRegex(
                    WorkspaceEconomicLockError,
                    "regular non-symlink file",
                ):
                    lock.acquire()

            self.assertTrue(inserted)
            self.assertTrue(lock_path.is_symlink())
            self.assertFalse(external.exists())
            self.assertIsNone(lock._handle)


if __name__ == "__main__":
    unittest.main()
