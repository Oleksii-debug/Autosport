"""Plan 6 §6А: restore an interrupted verified uninstall, never owner state."""
from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport import offline_release_install as installer
from autosport import offline_release_lifecycle as lifecycle
from autosport.offline_release_recovery import recover_interrupted_uninstall


class RecoverInterruptedUninstallTests(unittest.TestCase):
    SOURCE = "a" * 40

    def _quarantined(self, root: Path):
        archive = root / "original.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Autosport-V1/Autosport.exe", b"product")
            z.writestr("Autosport-V1/BUILD_INFO.json", b"{}")
            z.writestr("Autosport-V1/examples/demo.json", b"{}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        destination = root / "installed"
        trusted = {"status": "PASS", "source_sha": self.SOURCE, "package_sha256": digest}
        with patch.object(installer.release_package, "verify_windows_package", return_value=trusted):
            installer.install_verified_release(
                archive, destination,
                expected_source_sha=self.SOURCE,
                expected_package_sha256=digest,
            )
        quarantine = root / ".autosport-uninstall-test1234" / "verified-product"
        quarantine.parent.mkdir()
        destination.rename(quarantine)
        return archive, digest, destination, quarantine, trusted

    def _recover(self, archive, digest, destination, quarantine, trusted):
        with patch.object(lifecycle.release_package, "verify_windows_package", return_value=trusted):
            return recover_interrupted_uninstall(
                quarantine, destination, archive=archive,
                expected_source_sha=self.SOURCE,
                expected_package_sha256=digest,
            )

    def test_rehydrates_deleted_file_and_restores_exact_release(self):
        with tempfile.TemporaryDirectory() as temp:
            archive, digest, dest, quarant, trusted = self._quarantined(Path(temp))
            (quarant / "Autosport.exe").unlink()
            result = self._recover(archive, digest, dest, quarant, trusted)
            self.assertEqual(result["status"], "UNINSTALL_RECOVERED")
            self.assertEqual(result["restored_files"], 1)
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"product")
            self.assertFalse(quarant.exists())
            self.assertFalse(result["real_money_execution"])
            self.assertFalse(result["nvda_verified"])

    def test_receipt_missing_can_be_recreated_only_from_independent_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            archive, digest, dest, quarant, trusted = self._quarantined(Path(temp))
            (quarant / ".autosport-installed-release.json").unlink()
            result = self._recover(archive, digest, dest, quarant, trusted)
            self.assertEqual(result["restored_files"], 0)
            self.assertTrue((dest / ".autosport-installed-release.json").is_file())

    def test_private_extra_file_blocks_and_preserves_every_byte(self):
        with tempfile.TemporaryDirectory() as temp:
            archive, digest, dest, quarant, trusted = self._quarantined(Path(temp))
            private = quarant / "private-account.json"
            private.write_bytes(b"private owner data")
            with self.assertRaisesRegex(installer.OfflineInstallError, "unknown or unsafe"):
                self._recover(archive, digest, dest, quarant, trusted)
            self.assertEqual(private.read_bytes(), b"private owner data")
            self.assertFalse(dest.exists())

    def test_tampered_owned_file_blocks_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            archive, digest, dest, quarant, trusted = self._quarantined(Path(temp))
            (quarant / "Autosport.exe").write_bytes(b"counterfeit")
            with self.assertRaisesRegex(installer.OfflineInstallError, "mutated quarantine"):
                self._recover(archive, digest, dest, quarant, trusted)
            self.assertFalse(dest.exists())

    def test_forged_receipt_blocks_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            archive, digest, dest, quarant, trusted = self._quarantined(Path(temp))
            receipt = quarant / ".autosport-installed-release.json"
            receipt.write_text('{"source_sha":"forged"}', encoding="utf-8")
            with self.assertRaisesRegex(installer.OfflineInstallError, "receipt is invalid"):
                self._recover(archive, digest, dest, quarant, trusted)
            self.assertFalse(dest.exists())

    def test_preexisting_destination_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            archive, digest, dest, quarant, trusted = self._quarantined(Path(temp))
            dest.mkdir()
            (dest / "bankroll.txt").write_text("private")
            with self.assertRaisesRegex(installer.OfflineInstallError, "existing installation"):
                self._recover(archive, digest, dest, quarant, trusted)
            self.assertEqual((dest / "bankroll.txt").read_text(), "private")

    def test_wrong_archive_anchor_refuses_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            archive, digest, dest, quarant, trusted = self._quarantined(Path(temp))
            with self.assertRaises(installer.OfflineInstallError):
                self._recover(archive, "f" * 64, dest, quarant, trusted)
            self.assertFalse(dest.exists())

    def test_wrong_quarantine_layout_refuses_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            archive, digest, dest, quarant, trusted = self._quarantined(Path(temp))
            other = Path(temp) / "arbitrary" / "verified-product"
            other.parent.mkdir()
            quarant.rename(other)
            with self.assertRaisesRegex(installer.OfflineInstallError, "not canonical"):
                self._recover(archive, digest, dest, other, trusted)
            self.assertFalse(dest.exists())

    def test_symlinked_foreign_directory_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive, digest, dest, quarant, trusted = self._quarantined(root)
            external = root / "external"
            external.mkdir()
            (external / "private").write_text("intact")
            (quarant / "examples").rename(root / "temporary-examples")
            try:
                (quarant / "examples").symlink_to(external, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("OS does not permit symlink creation")
            with self.assertRaises(installer.OfflineInstallError):
                self._recover(archive, digest, dest, quarant, trusted)
            self.assertEqual((external / "private").read_text(), "intact")
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
