"""Focused negative/recovery checks for offline PAPER release clean installation."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport import offline_release_install as installer


class OfflineInstallTests(unittest.TestCase):
    SOURCE = "a" * 40

    def _package(self, parent: Path) -> tuple[Path, str]:
        archive = parent / "Autosport-V1-windows-x64.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Autosport-V1/Autosport.exe", b"fixture-exe")
            z.writestr("Autosport-V1/BUILD_INFO.json", b"{}")
            z.writestr("Autosport-V1/examples/demo.json", b"{}")
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def _install(self, archive: Path, destination: Path, digest: str) -> dict:
        result = {
            "status": "PASS", "package_sha256": digest, "source_sha": self.SOURCE
        }
        with patch.object(installer.release_package, "verify_windows_package", return_value=result) as verifier:
            output = installer.install_verified_release(
                archive, destination, expected_source_sha=self.SOURCE,
                expected_package_sha256=digest,
            )
            verifier.assert_called_once_with(
                archive, expected_source_sha=self.SOURCE, require_source_manifest=True,
            )
        return output

    def test_clean_install_publishes_product_tree_and_restricted_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            archive, digest = self._package(root)
            destination = root / "Autosport with spaces Ω"
            result = self._install(archive, destination, digest)
            self.assertEqual(result["status"], "INSTALLED")
            self.assertEqual((destination / "Autosport.exe").read_bytes(), b"fixture-exe")
            self.assertEqual((destination / "examples/demo.json").read_bytes(), b"{}")
            receipt = json.loads((destination / ".autosport-installed-release.json").read_text())
            self.assertEqual(receipt["package_sha256"], digest)
            self.assertEqual(receipt["files"]["Autosport.exe"], hashlib.sha256(b"fixture-exe").hexdigest())
            self.assertFalse(receipt["real_money_execution"])
            self.assertFalse(receipt["nvda_verified"])
            self.assertFalse(list(root.glob(".autosport-install-staging-*")))

    def test_existing_install_and_workspace_are_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            archive, digest = self._package(root)
            destination = root / "install"
            destination.mkdir()
            private = destination / "private-owner-state.json"
            private.write_text("original")
            with self.assertRaisesRegex(installer.OfflineInstallError, "existing installation"):
                self._install(archive, destination, digest)
            self.assertEqual(private.read_text(), "original")

    def test_wrong_digest_rejected_before_verifier_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            archive, _ = self._package(root)
            with patch.object(installer.release_package, "verify_windows_package") as verifier:
                with self.assertRaisesRegex(installer.OfflineInstallError, "independently pinned"):
                    installer.install_verified_release(
                        archive, root / "install", expected_source_sha=self.SOURCE,
                        expected_package_sha256="f" * 64,
                    )
                verifier.assert_not_called()
            self.assertFalse((root / "install").exists())

    def test_traversal_entry_fails_closed_without_publication(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("Autosport-V1/../outside.txt", "bad")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with self.assertRaisesRegex(installer.OfflineInstallError, "unsafe release path"):
                self._install(archive, root / "target", digest)
            self.assertFalse((root / "target").exists())
            self.assertFalse((root / "outside.txt").exists())

    def test_forged_source_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            archive, digest = self._package(root)
            forged = {"status": "PASS", "package_sha256": digest, "source_sha": "f" * 40}
            with patch.object(installer.release_package, "verify_windows_package", return_value=forged):
                with self.assertRaisesRegex(installer.OfflineInstallError, "trust anchors"):
                    installer.install_verified_release(
                        archive, root / "install", expected_source_sha=self.SOURCE,
                        expected_package_sha256=digest,
                    )
            self.assertFalse((root / "install").exists())

    def test_archive_tamper_during_extract_fails_without_publication(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            archive, digest = self._package(root)
            canonical = {"status": "PASS", "package_sha256": digest, "source_sha": self.SOURCE}
            original = installer._sha_file
            calls = []

            def tamper_on_second_read(path: Path) -> str:
                calls.append(path)
                return "0" * 64 if len(calls) == 2 else original(path)

            with patch.object(installer.release_package, "verify_windows_package", return_value=canonical):
                with patch.object(installer, "_sha_file", side_effect=tamper_on_second_read):
                    with self.assertRaisesRegex(installer.OfflineInstallError, "changed during"):
                        installer.install_verified_release(
                            archive, root / "install", expected_source_sha=self.SOURCE,
                            expected_package_sha256=digest,
                        )
            self.assertFalse((root / "install").exists())
            self.assertFalse(list(root.glob(".autosport-install-staging-*")))


    def test_symlink_in_ancestor_is_rejected_before_release_verification(self) -> None:
        """A safe-looking parent must not hide an earlier symlink component."""
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            actual = root / "actual" / "nested"
            actual.mkdir(parents=True)
            alias = root / "alias"
            try:
                alias.symlink_to(root / "actual", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable on this test host: {type(exc).__name__}")
            archive, digest = self._package(root)
            destination = alias / "nested" / "Autosport"
            with patch.object(installer.release_package, "verify_windows_package") as verifier:
                with self.assertRaisesRegex(installer.OfflineInstallError, "symlink"):
                    installer.install_verified_release(
                        archive, destination, expected_source_sha=self.SOURCE,
                        expected_package_sha256=digest,
                    )
                verifier.assert_not_called()
            self.assertFalse((root / "actual" / "nested" / "Autosport").exists())
            self.assertFalse(list(root.glob(".autosport-install-staging-*")))

    def test_regular_nested_parent_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            parent = root / "ordinary" / "nested"
            parent.mkdir(parents=True)
            archive, digest = self._package(root)
            result = self._install(archive, parent / "Autosport", digest)
            self.assertEqual(result["status"], "INSTALLED")
            self.assertTrue((parent / "Autosport" / "Autosport.exe").is_file())


if __name__ == "__main__":
    unittest.main()
