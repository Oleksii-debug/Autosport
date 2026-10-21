"""Exact installed PAPER release verification and safe uninstall regressions."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport import offline_release_install as install
from autosport import offline_release_lifecycle as lifecycle


class InstalledReleaseLifecycleTests(unittest.TestCase):
    SOURCE = "a" * 40

    def _installed(self, root: Path) -> tuple[Path, str]:
        archive = root / "source.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("Autosport-V1/Autosport.exe", b"safe-test-executable")
            package.writestr("Autosport-V1/BUILD_INFO.json", b"{}")
            package.writestr("Autosport-V1/examples/demo.json", b"{}")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        verified = {
            "status": "PASS",
            "package_sha256": digest,
            "source_sha": self.SOURCE,
        }
        destination = root / "installed"
        with patch.object(install.release_package, "verify_windows_package", return_value=verified):
            install.install_verified_release(
                archive, destination, expected_source_sha=self.SOURCE,
                expected_package_sha256=digest,
            )
        return destination, digest

    def _verify(self, directory: Path, digest: str) -> dict:
        return lifecycle.inspect_installed_release(
            directory, expected_source_sha=self.SOURCE,
            expected_package_sha256=digest,
        )

    def _uninstall(self, directory: Path, digest: str, archive: Path | None = None) -> dict:
        source = archive if archive is not None else directory.parent / "source.zip"
        evidence = {
            "status": "PASS", "source_sha": self.SOURCE, "package_sha256": digest,
        }
        with patch.object(
            lifecycle.release_package, "verify_windows_package", return_value=evidence
        ):
            return lifecycle.uninstall_verified_release(
                directory, original_archive=source,
                expected_source_sha=self.SOURCE, expected_package_sha256=digest,
            )

    def test_verifies_only_exact_pinned_installed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            report = self._verify(directory, digest)
            self.assertEqual(report["status"], "VERIFIED")
            self.assertEqual(report["file_count"], 3)
            self.assertFalse(report["real_money_execution"])
            self.assertFalse(report["human_tested"])
            self.assertFalse(report["nvda_verified"])
            self.assertTrue(directory.exists())

    def test_uninstalls_only_exact_product_and_keeps_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            report = self._uninstall(directory, digest)
            self.assertEqual(report["status"], "UNINSTALLED")
            self.assertFalse(directory.exists())
            self.assertTrue((root / "source.zip").exists())
            self.assertFalse(list(root.glob(".autosport-uninstall-*")))

    def test_rejects_unowned_user_data_without_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            extra = directory / "private-bankroll.json"
            extra.write_text("do-not-delete")
            with self.assertRaisesRegex(install.OfflineInstallError, "unowned files"):
                self._uninstall(directory, digest)
            self.assertEqual(extra.read_text(), "do-not-delete")
            self.assertTrue((directory / "Autosport.exe").is_file())

    def test_rejects_modified_release_file_without_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            (directory / "Autosport.exe").write_bytes(b"changed")
            with self.assertRaisesRegex(install.OfflineInstallError, "modified bytes"):
                self._uninstall(directory, digest)
            self.assertTrue((directory / "Autosport.exe").exists())

    def test_refuses_wrong_independent_package_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            with self.assertRaisesRegex(install.OfflineInstallError, "trusted anchors"):
                self._uninstall(directory, "f" * 64)
            self.assertTrue(directory.exists())

    def test_duplicate_receipt_keys_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            path = directory / ".autosport-installed-release.json"
            original = path.read_text()
            path.write_text(original.replace('"product":"Autosport"', '"product":"Autosport","product":"Fake"'))
            with self.assertRaisesRegex(install.OfflineInstallError, "invalid installed-release receipt"):
                self._uninstall(directory, digest)
            self.assertTrue(directory.exists())

    def test_symlink_payload_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            target = directory / "examples" / "demo.json"
            target.unlink()
            external = root / "private.txt"
            external.write_text("owner state")
            try:
                target.symlink_to(external)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks unavailable: {type(exc).__name__}")
            with self.assertRaisesRegex(install.OfflineInstallError, "modified bytes"):
                self._uninstall(directory, digest)
            self.assertEqual(external.read_text(), "owner state")

    def test_second_check_failure_restores_intact_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            genuine = lifecycle.inspect_installed_release
            calls = []

            def reject_second(path, **kwargs):
                calls.append(path)
                if len(calls) == 2:
                    raise install.OfflineInstallError("simulated moved-tree inspection failure")
                return genuine(path, **kwargs)

            with patch.object(lifecycle, "inspect_installed_release", side_effect=reject_second):
                with self.assertRaisesRegex(install.OfflineInstallError, "simulated"):
                    self._uninstall(directory, digest)
            self.assertTrue(directory.exists())
            self.assertEqual(self._verify(directory, digest)["status"], "VERIFIED")
            self.assertFalse(list(root.glob(".autosport-uninstall-*")))


    def test_self_rehashed_owner_file_in_forged_receipt_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            extra = directory / "owner-paper-portfolio.db"
            extra.write_bytes(b"must-stay")
            receipt_path = directory / ".autosport-installed-release.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["files"]["owner-paper-portfolio.db"] = hashlib.sha256(
                extra.read_bytes()
            ).hexdigest()
            receipt_path.write_text(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            # Without the original ZIP check, the self-authored receipt and
            # installed bytes satisfy the old local verifier.
            self.assertEqual(self._verify(directory, digest)["status"], "VERIFIED")
            with self.assertRaisesRegex(install.OfflineInstallError, "original canonical"):
                self._uninstall(directory, digest)
            self.assertEqual(extra.read_bytes(), b"must-stay")
            self.assertTrue((directory / "Autosport.exe").is_file())

    def test_replacement_archive_with_wrong_pinned_digest_cannot_remove_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            impostor = root / "different.zip"
            with zipfile.ZipFile(impostor, "w") as package:
                package.writestr("Autosport-V1/Autosport.exe", b"fake")
            with self.assertRaisesRegex(install.OfflineInstallError, "SHA-256 mismatch"):
                self._uninstall(directory, digest, archive=impostor)
            self.assertTrue(directory.is_dir())

    def test_canonical_cli_dispatches_verification_and_uninstall(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from autosport.cli import main as cli_main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            common = [
                "--destination", str(directory),
                "--source-sha", self.SOURCE,
                "--package-sha256", digest,
            ]
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli_main(["verify-installed-release", *common]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "VERIFIED")
            output = StringIO()
            verified_package = {
                "status": "PASS", "source_sha": self.SOURCE, "package_sha256": digest,
            }
            with patch.object(
                lifecycle.release_package, "verify_windows_package",
                return_value=verified_package,
            ):
                with redirect_stdout(output):
                    self.assertEqual(cli_main([
                        "uninstall-release", "--archive", str(root / "source.zip"), *common,
                    ]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "UNINSTALLED")
            self.assertFalse(directory.exists())

    def test_canonical_cli_does_not_echo_tampered_release_data(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from autosport.cli import main as cli_main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, digest = self._installed(root)
            marker = "private_value_must_not_be_returned"
            (directory / "private-extra.txt").write_text(marker)
            output = StringIO()
            with redirect_stdout(output):
                status = cli_main([
                    "uninstall-release", "--archive", str(root / "source.zip"),
                    "--destination", str(directory),
                    "--source-sha", self.SOURCE, "--package-sha256", digest,
                ])
            self.assertEqual(status, 3)
            self.assertIn("FAIL_CLOSED", output.getvalue())
            self.assertNotIn(marker, output.getvalue())
            self.assertTrue(directory.exists())


if __name__ == "__main__":
    unittest.main()
