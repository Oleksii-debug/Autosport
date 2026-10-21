"""Plan 6 §6А: only an unchanged release from pinned original bytes may be removed."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport import offline_release_install as installer
from autosport import offline_release_uninstall as uninstall


class SafeReleaseUninstallTests(unittest.TestCase):
    SOURCE = "b" * 40

    def _prepared(self, parent: Path) -> tuple[Path, Path, str]:
        archive = parent / "Autosport-original.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("Autosport-V1/Autosport.exe", b"trusted-binary")
            package.writestr("Autosport-V1/BUILD_INFO.json", b"{}")
            package.writestr("Autosport-V1/examples/sample.json", b"{}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        destination = parent / "my installed release"
        expected = {"status": "PASS", "package_sha256": digest, "source_sha": self.SOURCE}
        with patch.object(installer.release_package, "verify_windows_package", return_value=expected):
            installer.install_verified_release(
                archive, destination, expected_source_sha=self.SOURCE,
                expected_package_sha256=digest,
            )
        return archive, destination, digest

    def _uninstall(self, archive: Path, destination: Path, digest: str) -> dict[str, object]:
        expected = {"status": "PASS", "package_sha256": digest, "source_sha": self.SOURCE}
        with patch.object(uninstall.release_package, "verify_windows_package", return_value=expected) as check:
            report = uninstall.uninstall_verified_release(
                archive, destination, expected_source_sha=self.SOURCE,
                expected_package_sha256=digest,
            )
            check.assert_called_once_with(
                archive, expected_source_sha=self.SOURCE, require_source_manifest=True,
            )
        return report

    def test_exact_installed_release_can_be_uninstalled_without_staging_leftovers(self):
        with tempfile.TemporaryDirectory() as home:
            parent = Path(home)
            archive, destination, digest = self._prepared(parent)
            result = self._uninstall(archive, destination, digest)
            self.assertEqual(result["status"], "UNINSTALLED")
            self.assertEqual(result["files_removed"], 3)
            self.assertFalse(result["real_money_execution"])
            self.assertFalse(destination.exists())
            self.assertTrue(archive.is_file())
            self.assertEqual(list(parent.glob(".autosport-uninstall-staging-*")), [])

    def test_user_created_file_preserved_and_uninstall_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            parent = Path(home)
            archive, destination, digest = self._prepared(parent)
            private = destination / "user-portfolio.db"
            private.write_text("KEEP", encoding="utf-8")
            with self.assertRaisesRegex(installer.OfflineInstallError, "unrecognized"):
                self._uninstall(archive, destination, digest)
            self.assertEqual(private.read_text(encoding="utf-8"), "KEEP")
            self.assertTrue((destination / "Autosport.exe").exists())

    def test_unknown_empty_directory_blocks_uninstall(self):
        with tempfile.TemporaryDirectory() as home:
            archive, destination, digest = self._prepared(Path(home))
            (destination / "workspace").mkdir()
            with self.assertRaisesRegex(installer.OfflineInstallError, "unrecognized"):
                self._uninstall(archive, destination, digest)
            self.assertTrue((destination / "workspace").is_dir())

    def test_modified_release_member_blocks_uninstall(self):
        with tempfile.TemporaryDirectory() as home:
            archive, destination, digest = self._prepared(Path(home))
            executable = destination / "Autosport.exe"
            executable.write_bytes(b"changed")
            with self.assertRaisesRegex(installer.OfflineInstallError, "differs"):
                self._uninstall(archive, destination, digest)
            self.assertEqual(executable.read_bytes(), b"changed")

    def test_forged_receipt_matching_local_bytes_not_zip_is_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            archive, destination, digest = self._prepared(Path(home))
            extra = destination / "fake-owner-data.db"
            extra.write_bytes(b"not part of shipped release")
            receipt = destination / installer._RECEIPT
            forged = json.loads(receipt.read_text(encoding="utf-8"))
            forged["files"]["fake-owner-data.db"] = hashlib.sha256(extra.read_bytes()).hexdigest()
            receipt.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(installer.OfflineInstallError, "verified archive inventory"):
                self._uninstall(archive, destination, digest)
            self.assertTrue(extra.exists())

    def test_wrong_archive_digest_does_not_mutate_installation(self):
        with tempfile.TemporaryDirectory() as home:
            archive, destination, digest = self._prepared(Path(home))
            with self.assertRaisesRegex(installer.OfflineInstallError, "digest differs"):
                self._uninstall(archive, destination, "f" * 64)
            self.assertTrue(destination.is_dir())

    def test_duplicate_receipt_keys_do_not_authorize_removal(self):
        with tempfile.TemporaryDirectory() as home:
            archive, destination, digest = self._prepared(Path(home))
            receipt = destination / installer._RECEIPT
            value = receipt.read_text(encoding="utf-8")
            receipt.write_text(value.replace('"product":"Autosport"', '"product":"Autosport","product":"Autosport"'), encoding="utf-8")
            with self.assertRaisesRegex(installer.OfflineInstallError, "invalid installed receipt"):
                self._uninstall(archive, destination, digest)
            self.assertTrue(destination.is_dir())

    def test_symlink_extra_data_is_not_followed_or_removed(self):
        with tempfile.TemporaryDirectory() as home:
            parent = Path(home)
            archive, destination, digest = self._prepared(parent)
            external = parent / "external-private.txt"
            external.write_text("KEEP", encoding="utf-8")
            alias = destination / "external"
            try:
                alias.symlink_to(external)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            with self.assertRaisesRegex(installer.OfflineInstallError, "unsafe"):
                self._uninstall(archive, destination, digest)
            self.assertEqual(external.read_text(encoding="utf-8"), "KEEP")

    def test_failed_quarantine_recheck_restores_same_installation(self):
        with tempfile.TemporaryDirectory() as home:
            parent = Path(home)
            archive, destination, digest = self._prepared(parent)
            original = uninstall._installed_members
            calls = []

            def fail_second(root, **kwargs):
                calls.append(Path(root))
                if len(calls) == 2:
                    raise installer.OfflineInstallError("changed after quarantine")
                return original(root, **kwargs)

            with patch.object(uninstall, "_installed_members", side_effect=fail_second):
                with self.assertRaisesRegex(installer.OfflineInstallError, "changed after"):
                    self._uninstall(archive, destination, digest)
            self.assertTrue((destination / "Autosport.exe").is_file())
            self.assertEqual(list(parent.glob(".autosport-uninstall-staging-*")), [])

    def test_cli_uninstall_failure_returns_generic_nonsecret_status(self):
        with tempfile.TemporaryDirectory() as home:
            archive, destination, digest = self._prepared(Path(home))
            (destination / "owner-secret.txt").write_text("PRIVATE")
            with patch("builtins.print") as printed:
                code = uninstall.main([
                    "--archive", str(archive), "--destination", str(destination),
                    "--source-sha", self.SOURCE, "--package-sha256", digest,
                ])
            self.assertEqual(code, 3)
            self.assertIn("FAIL_CLOSED", printed.call_args.args[0])
            self.assertNotIn("PRIVATE", printed.call_args.args[0])
            self.assertTrue(destination.is_dir())


if __name__ == "__main__":
    unittest.main()
