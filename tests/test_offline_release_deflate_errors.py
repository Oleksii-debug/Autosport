"""Corrupt raw Deflate failures are typed, non-destructive offline release errors.

A ZIP can pass an initial independent SHA/manifest check, then fail while its
members are read.  Neither the package's decompressor diagnostic nor a user
path may escape the canonical CLI/report error boundary.  These use synthetic
verifier receipts, not real publisher signatures or a Windows/NVDA run.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import hashlib
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

from autosport import offline_release_install as installer
from autosport import offline_release_lifecycle as lifecycle


class OfflineReleaseDeflateBoundaryTests(unittest.TestCase):
    SOURCE = "a" * 40
    PRIVATE = "owner-private-path-do-not-disclose"

    def _archive(self, root: Path) -> tuple[Path, str, dict[str, str]]:
        archive = root / "test-release.zip"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("Autosport-V1/Autosport.exe", b"fixture-exe")
            output.writestr("Autosport-V1/BUILD_INFO.json", b"{}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        result = {"status": "PASS", "source_sha": self.SOURCE,
                  "package_sha256": digest}
        return archive, digest, result

    def _install(self, archive: Path, dest: Path, digest: str, evidence: dict[str, str]):
        with patch.object(installer.release_package, "verify_windows_package",
                          return_value=evidence):
            return installer.install_verified_release(
                archive, dest, expected_source_sha=self.SOURCE,
                expected_package_sha256=digest,
            )

    def test_direct_clean_install_types_deflate_failure_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest, evidence = self._archive(root)
            dest = root / "installation"
            with patch.object(installer.release_package, "verify_windows_package",
                              return_value=evidence):
                with patch.object(zipfile.ZipFile, "open",
                                  side_effect=zlib.error(self.PRIVATE)):
                    with self.assertRaises(installer.OfflineInstallError) as raised:
                        installer.install_verified_release(
                            archive, dest, expected_source_sha=self.SOURCE,
                            expected_package_sha256=digest,
                        )
            self.assertNotIn(self.PRIVATE, str(raised.exception))
            self.assertFalse(dest.exists())
            self.assertFalse(list(root.glob(".autosport-install-staging-*")))

    def test_cli_fails_closed_without_leaking_deflate_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest, evidence = self._archive(root)
            dest = root / "installation"
            output = StringIO()
            with patch.object(installer.release_package, "verify_windows_package",
                              return_value=evidence):
                with patch.object(zipfile.ZipFile, "open",
                                  side_effect=zlib.error(self.PRIVATE)):
                    with redirect_stdout(output):
                        result = installer.main([
                            "--archive", str(archive), "--destination", str(dest),
                            "--source-sha", self.SOURCE,
                            "--package-sha256", digest,
                        ])
            self.assertEqual(result, 3)
            self.assertIn("FAIL_CLOSED", output.getvalue())
            self.assertNotIn(self.PRIVATE, output.getvalue())
            self.assertFalse(dest.exists())

    def test_installed_readback_types_deflate_failure_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest, evidence = self._archive(root)
            dest = root / "installation"
            self._install(archive, dest, digest, evidence)
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"fixture-exe")
            with patch.object(lifecycle.release_package, "verify_windows_package",
                              return_value=evidence):
                with patch.object(zipfile.ZipFile, "open",
                                  side_effect=zlib.error(self.PRIVATE)):
                    with self.assertRaises(lifecycle.OfflineInstallError) as raised:
                        lifecycle.inspect_installed_release(
                            dest, archive=archive,
                            expected_source_sha=self.SOURCE,
                            expected_package_sha256=digest,
                        )
            self.assertNotIn(self.PRIVATE, str(raised.exception))
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"fixture-exe")
            self.assertFalse(list(root.glob(".autosport-uninstall-*")))

    def test_uninstall_deflate_failure_retains_whole_installed_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, digest, evidence = self._archive(root)
            dest = root / "installation"
            self._install(archive, dest, digest, evidence)
            with patch.object(lifecycle.release_package, "verify_windows_package",
                              return_value=evidence):
                with patch.object(zipfile.ZipFile, "open",
                                  side_effect=zlib.error(self.PRIVATE)):
                    with self.assertRaises(lifecycle.OfflineInstallError):
                        lifecycle.uninstall_verified_release(
                            dest, archive=archive,
                            expected_source_sha=self.SOURCE,
                            expected_package_sha256=digest,
                        )
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"fixture-exe")
            self.assertTrue((dest / ".autosport-installed-release.json").is_file())
            self.assertFalse(list(root.glob(".autosport-uninstall-*")))


if __name__ == "__main__":
    unittest.main()
