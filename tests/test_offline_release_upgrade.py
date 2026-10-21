"""Plan 6 §6А: pinned same-schema PAPER upgrades, rollback and recovery."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport import offline_release_install as install
from autosport import offline_release_upgrade as upgrade


class OfflineReleaseUpgradeTests(unittest.TestCase):
    OLD = "a" * 40
    NEW = "b" * 40

    def _archive(self, root, name, source, version, schema):
        path = root / name
        info = {
            "product": "Autosport", "version": version,
            "update_version": version, "update_schema": schema,
            "source_sha": source, "real_money_execution": False,
            "human_tested": False, "nvda_verified": False,
        }
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("Autosport-V1/Autosport.exe", source.encode())
            z.writestr("Autosport-V1/BUILD_INFO.json", json.dumps(info))
            z.writestr("Autosport-V1/examples/demo.txt", "paper-only")
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def _installed(self, root, old_version="1.0.0", new_version="1.1.0", old_schema=1, new_schema=1):
        old, od = self._archive(root, "old.zip", self.OLD, old_version, old_schema)
        new, nd = self._archive(root, "new.zip", self.NEW, new_version, new_schema)
        dest = root / "Autosport Product"
        with patch.object(install.release_package, "verify_windows_package", return_value={
            "status": "PASS", "source_sha": self.OLD, "package_sha256": od,
        }):
            install.install_verified_release(
                old, dest, expected_source_sha=self.OLD, expected_package_sha256=od,
            )
        return dest, old, od, new, nd

    def _args(self, old, od, new, nd):
        return dict(old_archive=old, old_source_sha=self.OLD,
                    old_package_sha256=od, new_archive=new,
                    new_source_sha=self.NEW, new_package_sha256=nd)

    def _trusted(self):
        # Only the full canonical ZIP verifier is stubbed for synthetic fixtures.
        # Real receipt, installed-tree checks and publisher remain exercised.
        def respond(path, **kwargs):
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            return {"status": "PASS", "source_sha": kwargs["expected_source_sha"],
                    "package_sha256": digest}
        return patch.object(install.release_package, "verify_windows_package", side_effect=respond)

    def test_update_preserves_previous_and_never_enables_money(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                result = upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
            self.assertEqual(result["status"], "UPDATED_PAPER_ONLY")
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())
            self.assertEqual((Path(result["previous_release_path"]) / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertFalse(result["schema_migrated"])
            self.assertFalse(result["real_money_execution"])

    def test_rollback_keeps_rejected_release_for_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
                result = upgrade.rollback_verified_paper_release(dest, **self._args(old, od, new, nd))
            self.assertEqual(result["status"], "ROLLED_BACK_PAPER_ONLY")
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertEqual((Path(result["rejected_release_path"]) / "Autosport.exe").read_bytes(), self.NEW.encode())

    def test_rejects_schema_change_same_version_and_downgrade(self):
        for oldv, newv, a, b in [
            ("1.0.0", "1.1.0", 1, 2), ("1.1.0", "1.0.0", 1, 1),
            ("1.0.0", "1.0.0", 1, 1),
        ]:
            with self.subTest(old=oldv, new=newv, schema=b):
                with tempfile.TemporaryDirectory() as temp:
                    dest, old, od, new, nd = self._installed(Path(temp), oldv, newv, a, b)
                    with self._trusted():
                        with self.assertRaisesRegex(install.OfflineInstallError, "nonprogressing"):
                            upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
                    self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())

    def test_owner_file_blocks_without_deleting(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            private = dest / "bankroll.json"
            private.write_text("owner-state")
            with self._trusted():
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
            self.assertEqual(private.read_text(), "owner-state")

    def test_crash_gap_restores_from_independently_pinned_original(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                with patch.object(upgrade, "_publish_directory_no_replace", side_effect=SystemExit(42)):
                    with self.assertRaises(SystemExit):
                        upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
                self.assertFalse(dest.exists())
                result = upgrade.recover_update_gap(
                    dest, old_archive=old, old_source_sha=self.OLD,
                    old_package_sha256=od,
                )
            self.assertEqual(result["status"], "OLD_RELEASE_RECOVERED")
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())

    def test_normal_publication_failure_restores_existing_release(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            original = upgrade._publish_directory_no_replace
            def reject_candidate(candidate, target):
                if Path(candidate).name == "candidate":
                    raise OSError("simulated publication failure")
                return original(candidate, target)
            with self._trusted():
                with patch.object(upgrade, "_publish_directory_no_replace", side_effect=reject_candidate):
                    with self.assertRaises(OSError):
                        upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())

    def test_modified_previous_release_refuses_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                result = upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
                (Path(result["previous_release_path"]) / "Autosport.exe").write_bytes(b"tampered")
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.rollback_verified_paper_release(dest, **self._args(old, od, new, nd))
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())


if __name__ == "__main__":
    unittest.main()
