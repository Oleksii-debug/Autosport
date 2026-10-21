"""Plan 6 6A: refuse Windows junction/reparse destinations during offline installation."""
from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport import offline_release_install as install


class OfflineInstallerReparseBoundaryTests(unittest.TestCase):
    @staticmethod
    def _mock_reparse_path(flagged: Path):
        original = Path.lstat

        def lstat(path: Path):
            info = original(path)
            if path == flagged:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        return patch.object(Path, "lstat", lstat)

    def test_clean_new_directory_is_admitted_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "Autosport"
            self.assertEqual(install._safe_root(target), target)
            self.assertFalse(target.exists())

    def test_windows_reparse_parent_is_rejected_even_if_directory_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "junct"
            parent.mkdir()
            with self._mock_reparse_path(parent):
                with self.assertRaisesRegex(install.OfflineInstallError, "unsafe"):
                    install._safe_root(parent / "Autosport")
            self.assertFalse((parent / "Autosport").exists())

    def test_windows_reparse_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            ancestor = Path(temp) / "junct"
            parent = ancestor / "child"
            parent.mkdir(parents=True)
            with self._mock_reparse_path(ancestor):
                with self.assertRaisesRegex(install.OfflineInstallError, "reparse|symlink"):
                    install._safe_root(parent / "Autosport")
            self.assertFalse((parent / "Autosport").exists())

    def test_reparse_destination_is_detected_even_when_not_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "old-install"
            target.mkdir()
            with self._mock_reparse_path(target):
                self.assertTrue(install._is_redirected_install_path(target))
                with self.assertRaises(install.OfflineInstallError):
                    install._safe_root(target)

    def test_uninspectable_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "parent"
            parent.mkdir()
            original = Path.lstat

            def lstat(path: Path):
                if path == parent:
                    raise PermissionError("untrusted redirected path")
                return original(path)

            with patch.object(Path, "lstat", lstat):
                with self.assertRaisesRegex(install.OfflineInstallError, "cannot be safely inspected"):
                    install._safe_root(parent / "Autosport")

    def test_missing_destination_is_not_reported_as_reparse(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(install._is_redirected_install_path(Path(temp) / "not-yet-created"))

    def test_real_symlink_stays_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            link = root / "link"
            try:
                link.symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this platform")
            self.assertTrue(install._is_redirected_install_path(link))
            with self.assertRaises(install.OfflineInstallError):
                install._safe_root(link / "new-release")


if __name__ == "__main__":
    unittest.main()
