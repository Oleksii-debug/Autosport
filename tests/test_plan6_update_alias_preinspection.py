"""Plan 6 §6А: lexical update path admission must precede installed reads."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport import offline_release_upgrade as upgrade
from autosport.offline_release_install import OfflineInstallError


class UpdateAliasPreinspectionTests(unittest.TestCase):
    def _operations(self, destination):
        old = dict(old_archive=Path("old.zip"), old_source_sha="a" * 40,
                   old_package_sha256="a" * 64)
        new = dict(new_archive=Path("new.zip"), new_source_sha="b" * 40,
                   new_package_sha256="b" * 64)
        return (
            ("upgrade", lambda: upgrade.upgrade_verified_paper_release(
                destination, **old, **new)),
            ("recover", lambda: upgrade.recover_update_gap(
                destination, **old)),
            ("rollback", lambda: upgrade.rollback_verified_paper_release(
                destination, **old, **new)),
            ("finalize", lambda: upgrade.finalize_verified_paper_update(
                destination, **old, **new)),
        )

    def test_parent_traversal_is_rejected_before_any_inspection_or_occupancy_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "unused-child" / ".." / "product"
            for name, run in self._operations(destination):
                with self.subTest(operation=name):
                    with patch.object(upgrade, "_inspection_root",
                                      side_effect=AssertionError("inspection before preflight")):
                        with patch.object(Path, "exists",
                                          side_effect=AssertionError("occupancy before preflight")):
                            with patch.object(Path, "is_symlink",
                                              side_effect=AssertionError("symlink probe before preflight")):
                                with self.assertRaisesRegex(
                                    OfflineInstallError, "parent traversal"
                                ):
                                    run()

    @unittest.skipUnless(os.name == "nt", "native Win32 spelling gate")
    def test_native_win32_aliases_are_rejected_before_inspection(self):
        for alias in (r"C:\Apps\Autosport Product.",
                      "C:\\Apps\\Autosport Product ",
                      r"C:\Apps.\Autosport Product",
                      r"C:\Apps \Autosport Product"):
            for name, run in self._operations(Path(alias)):
                with self.subTest(operation=name, alias=alias):
                    with patch.object(upgrade, "_inspection_root",
                                      side_effect=AssertionError("inspection before alias gate")):
                        with patch.object(Path, "exists",
                                          side_effect=AssertionError("occupancy before alias gate")):
                            with patch.object(Path, "is_symlink",
                                              side_effect=AssertionError("symlink probe before alias gate")):
                                with self.assertRaisesRegex(
                                    OfflineInstallError, "trailing-dot-or-space"
                                ):
                                    run()


if __name__ == "__main__":
    unittest.main()
