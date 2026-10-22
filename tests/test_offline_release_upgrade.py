"""Plan 6 §6А: pinned same-schema PAPER upgrades, rollback and recovery."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
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
                publish = upgrade._publish_directory_no_replace

                def interrupt_after_old_release_is_quarantined(source, target):
                    # The simulated crash must happen AFTER the incumbent
                    # moves to "previous", not during the first move.
                    if Path(source).name == "candidate" and Path(target) == dest:
                        self.assertTrue(
                            (upgrade._transaction_root(dest) / "previous" / "Autosport.exe").is_file()
                        )
                        raise SystemExit(42)
                    return publish(source, target)

                with patch.object(
                    upgrade, "_publish_directory_no_replace",
                    side_effect=interrupt_after_old_release_is_quarantined,
                ):
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


    def test_finalize_after_update_discards_only_verified_previous(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            args = self._args(old, od, new, nd)
            with self._trusted():
                prior = upgrade.upgrade_verified_paper_release(dest, **args)
                outcome = upgrade.finalize_verified_paper_update(dest, **args)
            self.assertEqual(outcome["status"], "PAPER_UPDATE_FINALIZED")
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())
            self.assertFalse(Path(prior["previous_release_path"]).exists())
            self.assertFalse(upgrade._transaction_root(dest).exists())

    def test_finalize_after_rollback_retains_restored_old_release(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            args = self._args(old, od, new, nd)
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **args)
                upgrade.rollback_verified_paper_release(dest, **args)
                outcome = upgrade.finalize_verified_paper_update(dest, **args)
            self.assertEqual(outcome["status"], "PAPER_UPDATE_FINALIZED")
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertFalse(upgrade._transaction_root(dest).exists())

    def test_finalize_refuses_unknown_transaction_content(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            args = self._args(old, od, new, nd)
            with self._trusted():
                prior = upgrade.upgrade_verified_paper_release(dest, **args)
                private = upgrade._transaction_root(dest) / "private-owner.txt"
                private.write_text("intact")
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.finalize_verified_paper_update(dest, **args)
            self.assertEqual(private.read_text(), "intact")
            self.assertTrue(Path(prior["previous_release_path"]).is_dir())


    def test_bounded_update_build_info_and_no_partial_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dest, old, od, new, nd = self._installed(root)
            # Large source metadata is independently hashable but not an
            # acceptable in-memory update identity, even if ZIP verifier is stubbed.
            with zipfile.ZipFile(new, "w") as z:
                z.writestr("Autosport-V1/Autosport.exe", self.NEW.encode())
                z.writestr("Autosport-V1/BUILD_INFO.json", " " * 9000)
                z.writestr("Autosport-V1/examples/demo.txt", "paper-only")
            nd = hashlib.sha256(new.read_bytes()).hexdigest()
            with self._trusted():
                with self.assertRaisesRegex(install.OfflineInstallError, "exceeds size bound"):
                    upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertFalse(upgrade._transaction_root(dest).exists())


    def test_rejects_alias_to_same_installation_transaction_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "alias-parent").mkdir()
            canonical = root / "Autosport Product"
            aliased = root / "alias-parent" / ".." / "Autosport Product"
            # Before this guard these identical real destinations obtained
            # different transaction hashes and could bypass the pending lock.
            self.assertEqual(canonical.resolve(), aliased.resolve())
            self.assertNotEqual(str(canonical), str(aliased))
            with self.assertRaisesRegex(
                install.OfflineInstallError, "parent traversal"
            ):
                upgrade._transaction_root(aliased)
            self.assertEqual(
                upgrade._transaction_root(canonical).parent, root
            )

    def test_upgrade_refuses_concurrent_previous_directory_without_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            previous = upgrade._transaction_root(dest) / "previous"
            publish = upgrade._publish_directory_no_replace

            def concurrent_previous(source, target):
                if Path(target) == previous:
                    previous.mkdir()
                return publish(source, target)

            with self._trusted():
                with patch.object(upgrade, "_publish_directory_no_replace", side_effect=concurrent_previous):
                    with self.assertRaises((install.OfflineInstallError, OSError)):
                        upgrade.upgrade_verified_paper_release(
                            dest, **self._args(old, od, new, nd)
                        )
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertTrue(previous.is_dir())
            self.assertEqual(list(previous.iterdir()), [])

    def test_rollback_refuses_concurrent_rejected_directory_without_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            rejected = upgrade._transaction_root(dest) / "rejected-current"
            publish = upgrade._publish_directory_no_replace
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))

                def concurrent_rejected(source, target):
                    if Path(target) == rejected:
                        rejected.mkdir()
                    return publish(source, target)

                with patch.object(upgrade, "_publish_directory_no_replace", side_effect=concurrent_rejected):
                    with self.assertRaises((install.OfflineInstallError, OSError)):
                        upgrade.rollback_verified_paper_release(
                            dest, **self._args(old, od, new, nd)
                        )
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())
            self.assertTrue(rejected.is_dir())
            self.assertEqual(list(rejected.iterdir()), [])
            self.assertEqual(
                (upgrade._transaction_root(dest) / "previous" / "Autosport.exe").read_bytes(),
                self.OLD.encode(),
            )


    def test_update_rejects_same_bytes_replaced_after_second_trusted_read(self):
        # Copied identical bytes must not gain authority from an earlier
        # successful validation of a different filesystem directory.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, new, nd = self._installed(work)
            detached = work / "detached-original"
            original_verify = upgrade.inspect_installed_release
            inspected = 0

            def replace_after_second_check(path, **kwargs):
                nonlocal inspected
                result = original_verify(path, **kwargs)
                if Path(path) == dest:
                    inspected += 1
                    if inspected == 2:
                        dest.rename(detached)
                        shutil.copytree(detached, dest)
                return result

            with self._trusted():
                with patch.object(upgrade, "inspect_installed_release", side_effect=replace_after_second_check):
                    with self.assertRaisesRegex(
                        install.OfflineInstallError, "identity changed"
                    ):
                        upgrade.upgrade_verified_paper_release(
                            dest, **self._args(old, od, new, nd)
                        )
            self.assertEqual(inspected, 2)
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertEqual((detached / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertFalse((upgrade._transaction_root(dest) / "previous").exists())

    def test_rollback_rejects_same_bytes_replaced_after_verified_read(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, new, nd = self._installed(work)
            detached = work / "detached-active"
            args = self._args(old, od, new, nd)
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **args)
                original_verify = upgrade.inspect_installed_release

                def replace_after_active_check(path, **kwargs):
                    result = original_verify(path, **kwargs)
                    if Path(path) == dest:
                        dest.rename(detached)
                        shutil.copytree(detached, dest)
                    return result

                with patch.object(upgrade, "inspect_installed_release", side_effect=replace_after_active_check):
                    with self.assertRaisesRegex(
                        install.OfflineInstallError, "identity changed"
                    ):
                        upgrade.rollback_verified_paper_release(dest, **args)
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())
            self.assertEqual((detached / "Autosport.exe").read_bytes(), self.NEW.encode())
            self.assertEqual(
                (upgrade._transaction_root(dest) / "previous" / "Autosport.exe").read_bytes(),
                self.OLD.encode(),
            )

    def test_windows_case_alias_uses_one_pending_update_namespace(self):
        # A case-only alternate spelling is the same Windows installation,
        # and must not evade the sibling pending-update transaction fence.
        first = PureWindowsPath("C:/Apps/AutoSport Product")
        second = PureWindowsPath(r"c:\\apps\\AUTOSPORT PRODUCT")
        self.assertNotEqual(str(first), str(second))
        self.assertEqual(first, second)
        first_txn = upgrade._transaction_root(first)
        second_txn = upgrade._transaction_root(second)
        self.assertEqual(first_txn, second_txn)
        self.assertEqual(first_txn.name, second_txn.name)

    def test_windows_extended_and_device_aliases_cannot_bypass_update_fence(self):
        # Both alternate spellings can address the same ordinary C: install.
        # Reject device-namespace input instead of allocating a second lease.
        ordinary = PureWindowsPath(r"C:\Apps\Autosport Product")
        self.assertTrue(ordinary.is_absolute())
        self.assertTrue(upgrade._transaction_root(ordinary).name.startswith(".autosport-update-"))
        for alias in (
            PureWindowsPath(r"\\?\C:\Apps\Autosport Product"),
            PureWindowsPath(r"\\.\C:\Apps\Autosport Product"),
            PureWindowsPath(r"\\?\UNC\server\share\Autosport"),
        ):
            self.assertTrue(alias.is_absolute())
            with self.subTest(alias=str(alias)):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "device namespace"
                ):
                    upgrade._transaction_root(alias)

    def test_windows_trailing_dot_or_space_aliases_cannot_bypass_update_fence(self):
        # Normal Win32 APIs strip trailing dots/spaces from path components.
        # Each variant could otherwise select a different pending-update
        # transaction for the same installed product.
        canonical = PureWindowsPath(r"C:\Apps\Autosport Product")
        self.assertTrue(upgrade._transaction_root(canonical).name.startswith(".autosport-update-"))
        for alias in (
            PureWindowsPath(r"C:\Apps\Autosport Product."),
            PureWindowsPath(r"C:\Apps\Autosport Product "),
            PureWindowsPath(r"C:\Apps.\Autosport Product"),
            PureWindowsPath(r"C:\Apps \Autosport Product"),
        ):
            with self.subTest(alias=str(alias)):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "trailing-dot-or-space"
                ):
                    upgrade._transaction_root(alias)

    def test_normal_windows_unc_update_namespace_still_supported(self):
        first = PureWindowsPath(r"\\server\share\Autosport")
        second = PureWindowsPath(r"\\SERVER\SHARE\AUTOSPORT")
        self.assertEqual(
            upgrade._transaction_root(first).name,
            upgrade._transaction_root(second).name,
        )

    def test_posix_case_distinction_retains_distinct_update_namespace(self):
            # Use a platform-independent POSIX path: Path(temp) becomes a
            # WindowsPath on Windows runners and correctly case-folds there.
            first = PurePosixPath("/opt/Autosport")
            second = PurePosixPath("/opt/autosport")
            self.assertNotEqual(
                upgrade._transaction_root(first).name,
                upgrade._transaction_root(second).name,
            )


if __name__ == "__main__":
    unittest.main()
