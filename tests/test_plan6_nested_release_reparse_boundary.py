"""Plan 6 §6A: nested Windows junction/reparse release safety regressions.

Synthetic st_file_attributes witnesses run on Linux and Windows without needing
privileges to create real Windows junctions. Native Windows tests remain distinct.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from autosport import offline_release_install as install
from autosport import offline_release_lifecycle as lifecycle
from autosport import offline_release_recovery as recovery


class NestedWindowsReparseReleaseTests(unittest.TestCase):
    SOURCE = "a" * 40

    def _package_and_install(self, root: Path):
        archive = root / "original.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Autosport-V1/Autosport.exe", b"exe")
            z.writestr("Autosport-V1/BUILD_INFO.json", b"{}")
            z.writestr("Autosport-V1/examples/demo.json", b"test")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        verified = {
            "status": "PASS", "source_sha": self.SOURCE,
            "package_sha256": digest,
        }
        destination = root / "installed"
        with patch.object(install.release_package, "verify_windows_package", return_value=verified):
            install.install_verified_release(
                archive, destination, expected_source_sha=self.SOURCE,
                expected_package_sha256=digest,
            )
        return archive, digest, destination, verified

    @staticmethod
    def _reparse(flagged: Path):
        original = Path.lstat

        def lstat(path: Path):
            info = original(path)
            if path == flagged:
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
                )
            return info

        return patch.object(Path, "lstat", lstat)

    def _inspect(self, archive, digest, directory, verified):
        with patch.object(lifecycle.release_package, "verify_windows_package", return_value=verified):
            return lifecycle.inspect_installed_release(
                directory, archive=archive,
                expected_source_sha=self.SOURCE, expected_package_sha256=digest,
            )

    def test_nested_junction_refused_before_member_hash_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, digest, installed, verified = self._package_and_install(Path(tmp))
            flagged = installed / "examples"
            child = flagged / "demo.json"
            original_hash = lifecycle._hash_file

            def hash_no_external_read(path: Path):
                if path == child:
                    self.fail("nested redirected bytes were read before admission")
                return original_hash(path)

            with self._reparse(flagged), patch.object(
                lifecycle, "_hash_file", side_effect=hash_no_external_read,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "redirected",
                ):
                    self._inspect(archive, digest, installed, verified)
            self.assertEqual(child.read_bytes(), b"test")

    def test_reparse_regular_file_refused_without_hashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, digest, installed, verified = self._package_and_install(Path(tmp))
            flagged = installed / "BUILD_INFO.json"
            original_hash = lifecycle._hash_file

            def hash_no_reparse(path: Path):
                if path == flagged:
                    self.fail("reparse file was opened before admission")
                return original_hash(path)

            with self._reparse(flagged), patch.object(
                lifecycle, "_hash_file", side_effect=hash_no_reparse,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "missing or modified",
                ):
                    self._inspect(archive, digest, installed, verified)
            self.assertEqual(flagged.read_bytes(), b"{}")

    def test_nested_quarantine_junction_blocks_restore_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, digest, installed, verified = self._package_and_install(root)
            quarantine = root / ".autosport-uninstall-abc123" / "verified-product"
            quarantine.parent.mkdir()
            installed.rename(quarantine)
            missing = quarantine / "examples" / "demo.json"
            missing.unlink()
            external = root / "outside"
            external.mkdir()
            private = external / "owner-state.txt"
            private.write_text("DO-NOT-TOUCH", encoding="utf-8")
            with self._reparse(quarantine / "examples"), patch.object(
                recovery.release_package, "verify_windows_package",
                return_value=verified,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "redirected",
                ):
                    recovery.recover_interrupted_uninstall(
                        quarantine, installed, archive=archive,
                        expected_source_sha=self.SOURCE,
                        expected_package_sha256=digest,
                    )
            self.assertFalse(missing.exists())
            self.assertFalse(installed.exists())
            self.assertEqual(private.read_text(encoding="utf-8"), "DO-NOT-TOUCH")

    def test_redirected_receipt_refused_before_inspection_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, digest, installed, verified = self._package_and_install(Path(tmp))
            receipt = installed / install._RECEIPT
            original_read = Path.read_bytes

            def deny_redirected_read(path: Path):
                if path == receipt:
                    self.fail("redirected receipt was read before admission")
                return original_read(path)

            with self._reparse(receipt), patch.object(
                Path, "read_bytes", deny_redirected_read,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "missing or invalid installed-release receipt",
                ):
                    self._inspect(archive, digest, installed, verified)
            self.assertTrue(receipt.is_file())

    def test_redirected_quarantine_receipt_refused_before_recovery_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, digest, installed, verified = self._package_and_install(root)
            quarantine = root / ".autosport-uninstall-abc123" / "verified-product"
            quarantine.parent.mkdir()
            installed.rename(quarantine)
            receipt = quarantine / install._RECEIPT
            original_read = Path.read_bytes

            def deny_redirected_read(path: Path):
                if path == receipt:
                    self.fail("redirected quarantine receipt was read")
                return original_read(path)

            with self._reparse(receipt), patch.object(
                Path, "read_bytes", deny_redirected_read,
            ), patch.object(
                recovery.release_package, "verify_windows_package",
                return_value=verified,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "redirected",
                ):
                    recovery.recover_interrupted_uninstall(
                        quarantine, installed, archive=archive,
                        expected_source_sha=self.SOURCE,
                        expected_package_sha256=digest,
                    )
            self.assertFalse(installed.exists())
            self.assertTrue(receipt.is_file())

    def test_ordinary_verified_install_remains_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, digest, installed, verified = self._package_and_install(Path(tmp))
            self.assertEqual(
                self._inspect(archive, digest, installed, verified)["status"],
                "VERIFIED",
            )


    def test_installed_receipt_reparse_refused_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, digest, installed, verified = self._package_and_install(Path(tmp))
            flagged = installed / ".autosport-installed-release.json"
            original_read = Path.read_bytes

            def read_no_receipt(path: Path):
                if path == flagged:
                    self.fail("reparse receipt was read before admission")
                return original_read(path)

            with self._reparse(flagged), patch.object(
                Path, "read_bytes", read_no_receipt,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "missing or invalid installed-release receipt",
                ):
                    self._inspect(archive, digest, installed, verified)
            self.assertTrue(flagged.exists())

    def test_recovery_receipt_reparse_refused_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, digest, installed, verified = self._package_and_install(root)
            quarantine = root / ".autosport-uninstall-abc123" / "verified-product"
            quarantine.parent.mkdir()
            installed.rename(quarantine)
            flagged = quarantine / ".autosport-installed-release.json"
            original_read = Path.read_bytes

            def read_no_receipt(path: Path):
                if path == flagged:
                    self.fail("quarantine reparse receipt was read before admission")
                return original_read(path)

            with self._reparse(flagged), patch.object(
                Path, "read_bytes", read_no_receipt,
            ), patch.object(
                recovery.release_package, "verify_windows_package",
                return_value=verified,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "receipt is unsafe",
                ):
                    recovery.recover_interrupted_uninstall(
                        quarantine, installed, archive=archive,
                        expected_source_sha=self.SOURCE,
                        expected_package_sha256=digest,
                    )
            self.assertTrue(quarantine.exists())
            self.assertFalse(installed.exists())




if __name__ == "__main__":
    unittest.main()
