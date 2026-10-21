"""Exact Windows namespace guard for independently pinned offline PAPER installs."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport import offline_release_install as install


class OfflineInstallWindowsNamespaceTests(unittest.TestCase):
    SOURCE = "a" * 40

    def _attempt(self, root: Path, extra: dict[str, bytes]) -> tuple[Path, str]:
        archive = root / "release.zip"
        with zipfile.ZipFile(archive, "w") as package:
            for name, payload in {
                "Autosport-V1/Autosport.exe": b"test-only",
                "Autosport-V1/BUILD_INFO.json": b"{}",
                **extra,
            }.items():
                package.writestr(name, payload)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        destination = root / "new-installation"
        with patch.object(
            install.release_package, "verify_windows_package",
            return_value={
                "status": "PASS",
                "source_sha": self.SOURCE,
                "package_sha256": digest,
            },
        ):
            outcome = install.install_verified_release(
                archive, destination, expected_source_sha=self.SOURCE,
                expected_package_sha256=digest,
            )
        return destination, outcome["status"]

    def test_rejects_windows_casefold_alias_even_if_preflight_is_stubbed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(
                install.OfflineInstallError, "ambiguous Windows release path"
            ):
                self._attempt(root, {
                    "Autosport-V1/guide/Readme.txt": b"A",
                    "Autosport-V1/GUIDE/readme.TXT": b"B",
                })
            self.assertFalse((root / "new-installation").exists())
            self.assertFalse(list(root.glob(".autosport-install-staging-*")))

    def test_reserves_generated_receipt_against_windows_casefold_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(
                install.OfflineInstallError, "ambiguous Windows release path"
            ):
                self._attempt(
                    root, {"Autosport-V1/.AUTOSPORT-INSTALLED-RELEASE.JSON": b"forged"}
                )
            self.assertFalse((root / "new-installation").exists())

    def test_rejects_reserved_device_name_even_with_stubbed_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(
                install.OfflineInstallError, "unsafe Windows release path"
            ):
                self._attempt(root, {"Autosport-V1/examples/CON.txt": b"unsafe"})
            self.assertFalse((root / "new-installation").exists())

    def test_valid_release_remains_installable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            destination, status = self._attempt(
                root, {"Autosport-V1/examples/readme.txt": b"example"}
            )
            self.assertEqual(status, "INSTALLED")
            self.assertEqual(
                (destination / "examples" / "readme.txt").read_bytes(), b"example"
            )
            self.assertTrue((destination / ".autosport-installed-release.json").is_file())


if __name__ == "__main__":
    unittest.main()
