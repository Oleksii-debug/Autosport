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

            original_open = Path.open
            inserted = False

            def open_after_race(path_obj, *args, **kwargs):
                nonlocal inserted
                if path_obj == lock_path and not inserted:
                    inserted = True
                    os.symlink(external, lock_path)
                return original_open(path_obj, *args, **kwargs)

            lock = WorkspaceEconomicLock(root)
            with mock.patch.object(Path, "open", autospec=True, side_effect=open_after_race):
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
