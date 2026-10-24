"""Plan 6 §6А: pinned same-schema PAPER upgrades, rollback and recovery."""
from __future__ import annotations

import hashlib
import json
import stat
import shutil
import tempfile
import unittest
import pytest
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

from autosport import offline_release_install as install
from autosport import offline_release_upgrade as upgrade
from autosport import offline_release_lifecycle as lifecycle


class OfflineReleaseUpgradeTests(unittest.TestCase):
    OLD = "a" * 40
    NEW = "b" * 40

    def test_complete_release_and_private_workspace_lifecycle(self):
        """Exercise one full install/update/rollback/uninstall/reinstall chain.

        The private workspace is separately backed up and restored to a fresh
        root; a product update must never convert its data into install files.
        Synthetic ZIP trust is stubbed, but real file/receipt/backup checks run.
        """
        from autosport import workspace_backup

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "Мій робочий стан"
            workspace.mkdir()
            private = workspace / "особисті налаштування.json"
            payload = '{"режим":"PAPER","ліміт":"0"}\n'.encode("utf-8")
            private.write_bytes(payload)
            backup = root / "workspace-backup.zip"
            snapshot = workspace_backup.backup_workspace(workspace, backup)
            self.assertEqual(snapshot["file_count"], 1)

            dest, old, old_digest, new, new_digest = self._installed(root)
            args = self._args(old, old_digest, new, new_digest)
            with self._trusted():
                updated = upgrade.upgrade_verified_paper_release(dest, **args)
                self.assertEqual(updated["status"], "UPDATED_PAPER_ONLY")
                self.assertFalse(updated["schema_migrated"])
                self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())

                rolled_back = upgrade.rollback_verified_paper_release(dest, **args)
                self.assertEqual(rolled_back["status"], "ROLLED_BACK_PAPER_ONLY")
                self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
                retired = upgrade.finalize_verified_paper_update(dest, **args)
                self.assertEqual(retired["status"], "PAPER_UPDATE_FINALIZED")
                self.assertFalse(upgrade._transaction_root(dest).exists())

                upgraded_again = upgrade.upgrade_verified_paper_release(dest, **args)
                self.assertEqual(upgraded_again["status"], "UPDATED_PAPER_ONLY")
                retired_again = upgrade.finalize_verified_paper_update(dest, **args)
                self.assertEqual(retired_again["status"], "PAPER_UPDATE_FINALIZED")
                self.assertFalse(upgrade._transaction_root(dest).exists())

                removed = lifecycle.uninstall_verified_release(
                    dest, archive=new, expected_source_sha=self.NEW,
                    expected_package_sha256=new_digest,
                )
                self.assertEqual(removed["status"], "UNINSTALLED")
                self.assertFalse(dest.exists())
                reinstalled = install.install_verified_release(
                    old, dest, expected_source_sha=self.OLD,
                    expected_package_sha256=old_digest,
                )
                self.assertEqual(reinstalled["status"], "INSTALLED")
                lifecycle.inspect_installed_release(
                    dest, archive=old, expected_source_sha=self.OLD,
                    expected_package_sha256=old_digest,
                )

            recovered_workspace = root / "Відновлений стан"
            restored = workspace_backup.restore_workspace(backup, recovered_workspace)
            self.assertEqual(restored["file_count"], 1)
            self.assertEqual((recovered_workspace / private.name).read_bytes(), payload)
            self.assertEqual(private.read_bytes(), payload)
            self.assertFalse(snapshot.get("real_money_execution", False))
            self.assertFalse(restored["real_money_execution"])

    def test_crash_between_release_moves_recovers_and_retires_candidate(self):
        """A simulated hard stop leaves previous+candidate; recovery is usable."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dest, old, old_digest, new, new_digest = self._installed(root)
            args = self._args(old, old_digest, new, new_digest)
            actual_publish = upgrade._publish_directory_no_replace

            def interrupted_publish(source, target):
                if Path(source) == upgrade._transaction_root(dest) / "candidate" and Path(target) == dest:
                    raise KeyboardInterrupt("simulated power loss")
                return actual_publish(source, target)

            with self._trusted():
                with patch.object(upgrade, "_publish_directory_no_replace",
                                  side_effect=interrupted_publish):
                    with self.assertRaises(KeyboardInterrupt):
                        upgrade.upgrade_verified_paper_release(dest, **args)
                txn = upgrade._transaction_root(dest)
                self.assertFalse(dest.exists())
                self.assertTrue((txn / "previous").is_dir())
                self.assertTrue((txn / "candidate").is_dir())

                recovered = upgrade.recover_update_gap(
                    dest, old_archive=old, old_source_sha=self.OLD,
                    old_package_sha256=old_digest,
                )
                self.assertEqual(recovered["status"], "OLD_RELEASE_RECOVERED")
                self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
                retired = upgrade.finalize_verified_paper_update(dest, **args)
                self.assertEqual(retired["status"], "PAPER_UPDATE_FINALIZED")
                self.assertFalse(txn.exists())
                self.assertFalse(retired["real_money_execution"])

    def test_directory_witness_rejects_windows_junction_reparse_flag(self):
        # Windows junctions can advertise directory mode and a stable inode.
        # Never let such an alias authorize the move/rollback of an install.
        fake = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700, st_dev=7, st_ino=9,
            st_file_attributes=0x400,
        )
        with patch.object(Path, "lstat", return_value=fake):
            with self.assertRaisesRegex(
                install.OfflineInstallError, "replaced by a redirect",
            ):
                upgrade._directory_witness(Path("/verified-paper"))
        # An ordinary directory with the same mode/inode remains admissible.
        fake.st_file_attributes = 0
        with patch.object(Path, "lstat", return_value=fake):
            self.assertEqual(
                upgrade._directory_witness(Path("/verified-paper")), (7, 9),
            )

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

    def test_directory_identity_rejects_zero_inode_before_update_or_rollback(self):
        # An OS/filesystem reporting inode=0 cannot distinguish an intact
        # install from a same-content directory replacement. Never approve a
        # destructive release move using an unavailable identity witness.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root.lstat()
            if type(original.st_ino) is int and original.st_ino > 0:
                self.assertEqual(
                    upgrade._directory_witness(root),
                    (original.st_dev, original.st_ino),
                )
            else:
                # The running filesystem may itself lack an inode witness.
                with self.assertRaises(install.OfflineInstallError):
                    upgrade._directory_witness(root)
            cases = (
                (original.st_dev, 0),
                (-1, original.st_ino),
                (original.st_dev, True),
            )
            for device, inode in cases:
                with self.subTest(device=device, inode=inode):
                    fake = SimpleNamespace(
                        st_mode=original.st_mode, st_dev=device, st_ino=inode
                    )
                    with patch.object(Path, "lstat", return_value=fake):
                        with self.assertRaisesRegex(
                            install.OfflineInstallError,
                            "directory identity unavailable",
                        ):
                            upgrade._directory_witness(root)

    def test_update_rejects_candidate_swap_at_verified_read_return(self):
        # A replacement can occur *after* the verifier has accepted a
        # candidate but *before* it returns to the updater. Its identity
        # must already have been witnessed before that trusted inspection.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            candidate = txn / "candidate"
            real_inspection = upgrade.inspect_installed_release

            def replace_as_candidate_verification_returns(root, **kwargs):
                report = real_inspection(root, **kwargs)
                if Path(root) == candidate:
                    candidate.rename(txn / "verified-but-replaced")
                    candidate.mkdir()
                    (candidate / "Autosport.exe").write_bytes(b"forged-new-binary")
                return report

            with self._trusted(), patch.object(
                upgrade, "inspect_installed_release",
                side_effect=replace_as_candidate_verification_returns,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "staged release identity changed",
                ):
                    upgrade.upgrade_verified_paper_release(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertFalse((txn / "previous").exists())

    def test_update_rejects_staged_directory_replacement_before_old_release_moves(self):
        # A syntactically valid, previously verified candidate can be
        # replaced by an unverified directory when the old root is reread.
        # The update must not publish that replacement or move old bytes.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            candidate = txn / "candidate"
            real_inspection = upgrade.inspect_installed_release

            def swap_after_old_read(root, **kwargs):
                result = real_inspection(root, **kwargs)
                if Path(root) == dest and candidate.is_dir():
                    candidate.rename(txn / "checked-but-replaced")
                    candidate.mkdir()
                    (candidate / "Autosport.exe").write_bytes(b"unverified")
                return result

            with self._trusted(), patch.object(
                upgrade, "inspect_installed_release", side_effect=swap_after_old_read,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "staged release identity changed",
                ):
                    upgrade.upgrade_verified_paper_release(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertFalse((txn / "previous").exists())

    def test_update_restores_old_release_on_staged_replacement_after_quarantine(self):
        # Publication is two moves. Replacing candidate between the two must
        # fail closed and return the exact old directory to its original path.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            candidate = txn / "candidate"
            real_publish = upgrade._publish_directory_no_replace

            def replace_after_old_move(source, target):
                real_publish(source, target)
                if Path(source) == dest and Path(target) == txn / "previous":
                    candidate.rename(txn / "checked-but-replaced")
                    candidate.mkdir()
                    (candidate / "Autosport.exe").write_bytes(b"unverified")

            with self._trusted(), patch.object(
                upgrade, "_publish_directory_no_replace",
                side_effect=replace_after_old_move,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "staged release identity changed",
                ):
                    upgrade.upgrade_verified_paper_release(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertFalse((txn / "previous").exists())

    def test_update_rejects_in_place_staged_file_tampering_after_old_move(self):
        # Unlike a directory swap, overwriting one verified file does NOT
        # change the staged directory's inode witness. Reject such bytes
        # before publishing them and restore the exact old PAPER executable.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            candidate = txn / "candidate"
            original_publish = upgrade._publish_directory_no_replace
            seen_old_move = False

            def overwrite_staged_file_after_quarantine(source, target):
                nonlocal seen_old_move
                original_publish(source, target)
                if Path(source) == dest and Path(target) == txn / "previous":
                    seen_old_move = True
                    (candidate / "Autosport.exe").write_bytes(b"unauthorized-new-payload")

            with self._trusted(), patch.object(
                upgrade, "_publish_directory_no_replace",
                side_effect=overwrite_staged_file_after_quarantine,
            ):
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.upgrade_verified_paper_release(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertTrue(seen_old_move)
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertFalse((txn / "previous").exists())
            self.assertEqual(
                (candidate / "Autosport.exe").read_bytes(), b"unauthorized-new-payload",
            )

    def test_update_rejects_inplace_payload_change_during_final_publication(self):
        # The candidate is clean before its no-replace move. An in-place
        # rewrite immediately after that move retains the directory inode;
        # checking only directory identity used to return false success.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            real_publish = upgrade._publish_directory_no_replace
            committed = []
            corrupt = b"forged-post-publish-executable"

            def corrupt_after_publishing_candidate(source, target):
                real_publish(source, target)
                if Path(source).name == "candidate" and Path(target) == dest:
                    committed.append(True)
                    (dest / "Autosport.exe").write_bytes(corrupt)

            with self._trusted(), patch.object(
                upgrade, "_publish_directory_no_replace",
                side_effect=corrupt_after_publishing_candidate,
            ):
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.upgrade_verified_paper_release(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertEqual(committed, [True])
            txn = upgrade._transaction_root(dest)
            self.assertEqual((dest / "Autosport.exe").read_bytes(), corrupt)
            self.assertEqual(
                (txn / "previous" / "Autosport.exe").read_bytes(),
                self.OLD.encode(),
            )

    def test_update_refuses_corrupted_previous_during_quarantine(self):
        # The old directory inode survives an in-place rewrite immediately
        # after its move to the transaction. Success would leave no valid
        # rollback source, despite an otherwise verified new release.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            previous = txn / "previous"
            original_publish = upgrade._publish_directory_no_replace
            mutated = []

            def corrupt_old_after_move(source, target):
                result = original_publish(source, target)
                if Path(source) == dest and Path(target) == previous:
                    (previous / "Autosport.exe").write_bytes(b"corrupt-old-release")
                    mutated.append(True)
                return result

            with self._trusted(), patch.object(
                upgrade, "_publish_directory_no_replace",
                side_effect=corrupt_old_after_move,
            ):
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.upgrade_verified_paper_release(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertEqual(mutated, [True])
            self.assertFalse(dest.exists())
            self.assertEqual(
                (previous / "Autosport.exe").read_bytes(), b"corrupt-old-release",
            )
            self.assertTrue((txn / "candidate" / "Autosport.exe").is_file())

    def test_update_does_not_restore_corrupt_previous_after_candidate_failure(self):
        # A candidate integrity failure used to restore 'previous' without
        # checking its bytes. A concurrent old-release rewrite must instead
        # remain quarantined and must not be installed by the error handler.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            previous = txn / "previous"
            candidate = txn / "candidate"
            original_publish = upgrade._publish_directory_no_replace
            mutated = []

            def corrupt_both_after_move(source, target):
                result = original_publish(source, target)
                if Path(source) == dest and Path(target) == previous:
                    (previous / "Autosport.exe").write_bytes(b"bad-old")
                    (candidate / "Autosport.exe").write_bytes(b"bad-new")
                    mutated.append(True)
                return result

            with self._trusted(), patch.object(
                upgrade, "_publish_directory_no_replace",
                side_effect=corrupt_both_after_move,
            ):
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.upgrade_verified_paper_release(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertEqual(mutated, [True])
            self.assertFalse(dest.exists())
            self.assertEqual((previous / "Autosport.exe").read_bytes(), b"bad-old")
            self.assertEqual((candidate / "Autosport.exe").read_bytes(), b"bad-new")

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

    def test_rollback_refuses_false_forward_lineage_before_moving_files(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                upgrade.upgrade_verified_paper_release(
                    dest, **self._args(old, od, new, nd),
                )
                txn = upgrade._transaction_root(dest)
                before_current = (dest / "Autosport.exe").read_bytes()
                before_previous = (txn / "previous" / "Autosport.exe").read_bytes()
                with patch.object(
                    upgrade, "_identity",
                    side_effect=[((2, 0, 0), 1), ((1, 0, 0), 1)],
                ):
                    with self.assertRaisesRegex(
                        install.OfflineInstallError, "forward same-schema lineage",
                    ):
                        upgrade.rollback_verified_paper_release(
                            dest, **self._args(old, od, new, nd),
                        )
                self.assertEqual((dest / "Autosport.exe").read_bytes(), before_current)
                self.assertEqual((txn / "previous" / "Autosport.exe").read_bytes(), before_previous)
                self.assertFalse((txn / "rejected-current").exists())

    def test_finalize_refuses_schema_mismatch_before_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                upgrade.upgrade_verified_paper_release(
                    dest, **self._args(old, od, new, nd),
                )
                txn = upgrade._transaction_root(dest)
                with patch.object(
                    upgrade, "_identity",
                    side_effect=[((1, 0, 0), 1), ((1, 1, 0), 2)],
                ):
                    with self.assertRaisesRegex(
                        install.OfflineInstallError, "forward same-schema lineage",
                    ):
                        upgrade.finalize_verified_paper_update(
                            dest, **self._args(old, od, new, nd),
                        )
                self.assertTrue((txn / "previous" / "Autosport.exe").is_file())
                self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())

    def test_recovery_rejects_inplace_rewrite_after_restoration_move(self):
        # The restored directory remains the same inode while its executable
        # is overwritten as the atomic move completes. Never call this a
        # successful crash recovery or clear its update-transaction lease.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            dest.rename(txn / "previous")
            real_publish = upgrade._publish_directory_no_replace
            mutated = []

            def rewrite_after_recovery(source, target):
                real_publish(source, target)
                if Path(source) == txn / "previous" and Path(target) == dest:
                    mutated.append(True)
                    (dest / "Autosport.exe").write_bytes(b"corrupted-restored-payload")

            with self._trusted(), patch.object(
                upgrade, "_publish_directory_no_replace", side_effect=rewrite_after_recovery,
            ):
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.recover_update_gap(
                        dest, old_archive=old, old_source_sha=self.OLD,
                        old_package_sha256=od,
                    )
            self.assertEqual(mutated, [True])
            self.assertTrue(txn.is_dir(), "uncertain restoration keeps recovery lease")
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"corrupted-restored-payload")

    def test_rollback_rejects_inplace_rewrite_after_final_move(self):
        # Reusing the verified previous directory's inode is insufficient if
        # the active executable changes as the rollback move completes.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
            txn = upgrade._transaction_root(dest)
            real_publish = upgrade._publish_directory_no_replace
            mutated = []

            def rewrite_after_rollback(source, target):
                real_publish(source, target)
                if Path(source) == txn / "previous" and Path(target) == dest:
                    mutated.append(True)
                    (dest / "Autosport.exe").write_bytes(b"corrupted-rollback-payload")

            with self._trusted(), patch.object(
                upgrade, "_publish_directory_no_replace", side_effect=rewrite_after_rollback,
            ):
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.rollback_verified_paper_release(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertEqual(mutated, [True])
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"corrupted-rollback-payload")
            self.assertEqual(
                (txn / "rejected-current" / "Autosport.exe").read_bytes(),
                self.NEW.encode(),
            )

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

    def test_crash_recovery_retires_empty_lease_and_allows_next_upgrade(self):
        # A successful recovery with no candidate/quarantine left must not
        # permanently lock this exact installation out of future upgrades.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, new, nd = self._installed(work)
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            dest.rename(txn / "previous")
            with self._trusted():
                result = upgrade.recover_update_gap(
                    dest, old_archive=old, old_source_sha=self.OLD,
                    old_package_sha256=od,
                )
                self.assertEqual(result["status"], "OLD_RELEASE_RECOVERED")
                self.assertFalse(txn.exists(), "an empty recovery lease must retire")
                updated = upgrade.upgrade_verified_paper_release(
                    dest, **self._args(old, od, new, nd)
                )
            self.assertEqual(updated["status"], "UPDATED_PAPER_ONLY")
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())

    def test_recovery_rejects_executable_corruption_during_lease_retirement(self):
        # A valid restored product can be corrupted during the last rmdir.
        # Recovery must not report success after the last filesystem mutation.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, _new, _nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            dest.rename(txn / "previous")
            original_rmdir = Path.rmdir

            def corrupt_on_retirement(path):
                result = original_rmdir(path)
                if Path(path) == txn:
                    (dest / "Autosport.exe").write_bytes(b"corrupted-after-recovery")
                return result

            with self._trusted(), patch.object(Path, "rmdir", new=corrupt_on_retirement):
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.recover_update_gap(
                        dest, old_archive=old, old_source_sha=self.OLD,
                        old_package_sha256=od,
                    )
            self.assertFalse(txn.exists())
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"corrupted-after-recovery")

    def test_recovery_rejects_directory_identity_drift_during_lease_retirement(self):
        # Exercise the post-rmdir directory identity check deterministically,
        # without depending on whether Windows permits an open directory swap.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, _new, _nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            dest.rename(txn / "previous")
            original_rmdir = Path.rmdir
            original_witness = upgrade._directory_witness
            retired = False

            def retire_and_mark(path):
                nonlocal retired
                result = original_rmdir(path)
                if Path(path) == txn:
                    retired = True
                return result

            def drifted_witness(path):
                witness = original_witness(path)
                if Path(path) == dest and retired:
                    return witness[0], witness[1] + 1
                return witness

            with self._trusted(), patch.object(
                Path, "rmdir", new=retire_and_mark,
            ), patch.object(
                upgrade, "_directory_witness", side_effect=drifted_witness,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "restored release identity changed",
                ):
                    upgrade.recover_update_gap(
                        dest, old_archive=old, old_source_sha=self.OLD,
                        old_package_sha256=od,
                    )
            self.assertFalse(txn.exists())
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())

    def test_recovery_rejects_replaced_transaction_with_same_previous_inode(self):
        # Previous release remains the SAME filesystem object while its
        # transaction parent is replaced with a different directory.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, _new, _nd = self._installed(work)
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            previous = txn / "previous"
            dest.rename(previous)
            original_txn = upgrade._directory_witness(txn)
            detached = work / "detached-original-transaction"
            original_inspect = upgrade.inspect_installed_release
            swapped = False

            def swap_parent_after_first_inspection(path, **kwargs):
                nonlocal swapped
                report = original_inspect(path, **kwargs)
                if Path(path) == previous and not swapped:
                    txn.rename(detached)
                    txn.mkdir(mode=0o700)
                    (detached / "previous").rename(previous)
                    swapped = True
                    self.assertNotEqual(upgrade._directory_witness(txn), original_txn)
                return report

            with self._trusted(), patch.object(
                upgrade, "inspect_installed_release",
                side_effect=swap_parent_after_first_inspection,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "update transaction identity changed",
                ):
                    upgrade.recover_update_gap(
                        dest, old_archive=old, old_source_sha=self.OLD,
                        old_package_sha256=od,
                    )
            self.assertTrue(swapped)
            self.assertFalse(dest.exists())
            self.assertEqual((previous / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertTrue(detached.is_dir())

    def test_recovery_rejects_foreign_transaction_after_publication(self):
        # A foreign lease with private data must not be silently treated as a
        # legitimate retained candidate after the old product is restored.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, _new, _nd = self._installed(work)
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            previous = txn / "previous"
            dest.rename(previous)
            detached = work / "detached-original-transaction"
            publish = upgrade._publish_directory_no_replace
            injected = False

            def replace_parent_after_publish(source, target):
                nonlocal injected
                result = publish(source, target)
                if Path(source) == previous and Path(target) == dest:
                    txn.rename(detached)
                    txn.mkdir(mode=0o700)
                    (txn / "private.txt").write_bytes(b"foreign-owner-bytes")
                    injected = True
                return result

            with self._trusted(), patch.object(
                upgrade, "_publish_directory_no_replace",
                side_effect=replace_parent_after_publish,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "update transaction identity changed",
                ):
                    upgrade.recover_update_gap(
                        dest, old_archive=old, old_source_sha=self.OLD,
                        old_package_sha256=od,
                    )
            self.assertTrue(injected)
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertEqual((txn / "private.txt").read_bytes(), b"foreign-owner-bytes")
            self.assertTrue(detached.is_dir())

    def test_crash_recovery_keeps_unverified_candidate_for_disposition(self):
        # Recovery must never remove leftover staged release/private debris
        # merely to obtain an empty transaction namespace.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, _new, _nd = self._installed(work)
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            dest.rename(txn / "previous")
            candidate = txn / "candidate"
            candidate.mkdir()
            (candidate / "unverified.bin").write_bytes(b"owner-review-required")
            with self._trusted():
                result = upgrade.recover_update_gap(
                    dest, old_archive=old, old_source_sha=self.OLD,
                    old_package_sha256=od,
                )
            self.assertEqual(result["status"], "OLD_RELEASE_RECOVERED")
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertEqual(
                (candidate / "unverified.bin").read_bytes(), b"owner-review-required"
            )
            self.assertTrue(txn.is_dir())

    def test_recovery_rejects_in_place_previous_file_mutation_after_first_read(self):
        # Mutating Autosport.exe does not change its parent directory inode.
        # The final read must reject the changed bytes before restoring it.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, _new, _nd = self._installed(work)
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            previous = txn / "previous"
            dest.rename(previous)
            original_inspect = upgrade.inspect_installed_release
            reads = 0

            def tamper_after_first_read(path, **kwargs):
                nonlocal reads
                result = original_inspect(path, **kwargs)
                if Path(path) == previous:
                    reads += 1
                    if reads == 1:
                        witness = upgrade._directory_witness(previous)
                        (previous / "Autosport.exe").write_bytes(b"forged-after-read")
                        self.assertEqual(upgrade._directory_witness(previous), witness)
                return result

            with self._trusted(), patch.object(
                upgrade, "inspect_installed_release",
                side_effect=tamper_after_first_read,
            ):
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.recover_update_gap(
                        dest, old_archive=old, old_source_sha=self.OLD,
                        old_package_sha256=od,
                    )
            self.assertEqual(reads, 1)
            self.assertFalse(dest.exists())
            self.assertTrue(previous.is_dir())

    def test_rollback_rejects_in_place_previous_file_mutation_before_publication(self):
        # Rollback must restore the incumbent if the previously verified old
        # executable was altered in place before its last publication check.
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            args = self._args(old, od, new, nd)
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **args)
                previous = upgrade._transaction_root(dest) / "previous"
                original_inspect = upgrade.inspect_installed_release
                reads = 0

                def tamper_after_first_previous_read(path, **kwargs):
                    nonlocal reads
                    result = original_inspect(path, **kwargs)
                    if Path(path) == previous:
                        reads += 1
                        if reads == 1:
                            witness = upgrade._directory_witness(previous)
                            (previous / "Autosport.exe").write_bytes(b"forged-before-rollback")
                            self.assertEqual(upgrade._directory_witness(previous), witness)
                    return result

                with patch.object(
                    upgrade, "inspect_installed_release",
                    side_effect=tamper_after_first_previous_read,
                ):
                    with self.assertRaises(install.OfflineInstallError):
                        upgrade.rollback_verified_paper_release(dest, **args)
            self.assertEqual(reads, 1)
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())
            self.assertTrue(previous.is_dir())

    def test_recovery_rejects_same_bytes_replaced_after_trusted_read(self):
        # A byte-identical second directory is not the filesystem object
        # validated by the canonical recovery read.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, _new, _nd = self._installed(work)
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            previous = txn / "previous"
            dest.rename(previous)
            detached = work / "original-previous"
            original_verify = upgrade.inspect_installed_release
            checks = 0

            def replace_verified_previous(path, **kwargs):
                nonlocal checks
                report = original_verify(path, **kwargs)
                if Path(path) == previous:
                    checks += 1
                    previous.rename(detached)
                    shutil.copytree(detached, previous)
                return report

            with self._trusted():
                with patch.object(
                    upgrade, "inspect_installed_release",
                    side_effect=replace_verified_previous,
                ):
                    with self.assertRaisesRegex(
                        install.OfflineInstallError, "identity changed"
                    ):
                        upgrade.recover_update_gap(
                            dest, old_archive=old, old_source_sha=self.OLD,
                            old_package_sha256=od,
                        )
            self.assertEqual(checks, 1)
            self.assertFalse(dest.exists())
            self.assertEqual((previous / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertEqual((detached / "Autosport.exe").read_bytes(), self.OLD.encode())

    def test_recovery_rejects_symlink_substituted_after_trusted_read(self):
        # The post-read identity guard must not publish a redirect into the
        # nominally empty installed-product directory.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, _new, _nd = self._installed(work)
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            previous = txn / "previous"
            dest.rename(previous)
            detached = work / "original-previous"
            original_verify = upgrade.inspect_installed_release

            def redirect_verified_previous(path, **kwargs):
                report = original_verify(path, **kwargs)
                if Path(path) == previous:
                    previous.rename(detached)
                    try:
                        previous.symlink_to(detached, target_is_directory=True)
                    except (OSError, NotImplementedError) as exc:
                        self.skipTest(f"directory symlink creation unavailable: {type(exc).__name__}")
                return report

            with self._trusted():
                with patch.object(
                    upgrade, "inspect_installed_release",
                    side_effect=redirect_verified_previous,
                ):
                    with self.assertRaisesRegex(
                        install.OfflineInstallError, "redirect"
                    ):
                        upgrade.recover_update_gap(
                            dest, old_archive=old, old_source_sha=self.OLD,
                            old_package_sha256=od,
                        )
            self.assertFalse(dest.exists())
            self.assertTrue(previous.is_symlink())
            self.assertEqual((detached / "Autosport.exe").read_bytes(), self.OLD.encode())

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


    def test_finalize_rejects_same_bytes_transaction_swap_before_disposal(self):
        # A copied transaction has valid product bytes but is not the lease
        # whose identity was bound at admission. Do not delete its contents.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, new, nd = self._installed(work)
            args = self._args(old, od, new, nd)
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **args)
                txn = upgrade._transaction_root(dest)
                detached = work / "original-update-transaction"
                replacement = work / "copied-update-transaction"
                original_lineage = upgrade._require_forward_update_lineage
                swapped = []

                def replace_after_lineage(**kwargs):
                    result = original_lineage(**kwargs)
                    shutil.copytree(txn, replacement)
                    txn.rename(detached)
                    replacement.rename(txn)
                    swapped.append(True)
                    return result

                with patch.object(upgrade, "_require_forward_update_lineage",
                                  side_effect=replace_after_lineage):
                    with self.assertRaisesRegex(
                        install.OfflineInstallError,
                        "update transaction identity changed",
                    ):
                        upgrade.finalize_verified_paper_update(dest, **args)
            self.assertEqual(swapped, [True])
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())
            self.assertEqual((txn / "previous" / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertEqual((detached / "previous" / "Autosport.exe").read_bytes(), self.OLD.encode())

    def test_finalize_rejects_same_bytes_obsolete_swap_during_inspection(self):
        # The obsolete directory itself must be the inspected object even
        # when the transaction directory inode remains unchanged.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, new, nd = self._installed(work)
            args = self._args(old, od, new, nd)
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **args)
                obsolete = upgrade._transaction_root(dest) / "previous"
                detached = work / "original-obsolete"
                original_inspect = upgrade.inspect_installed_release
                swapped = []

                def replace_after_obsolete_inspection(path, **kwargs):
                    result = original_inspect(path, **kwargs)
                    if Path(path) == obsolete and not swapped:
                        obsolete.rename(detached)
                        shutil.copytree(detached, obsolete)
                        swapped.append(True)
                    return result

                with patch.object(upgrade, "inspect_installed_release",
                                  side_effect=replace_after_obsolete_inspection):
                    with self.assertRaisesRegex(
                        install.OfflineInstallError,
                        "obsolete release identity changed",
                    ):
                        upgrade.finalize_verified_paper_update(dest, **args)
            self.assertEqual(swapped, [True])
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())
            self.assertEqual((obsolete / "Autosport.exe").read_bytes(), self.OLD.encode())
            self.assertEqual((detached / "Autosport.exe").read_bytes(), self.OLD.encode())

    def test_finalize_rejects_transaction_swap_after_disposal(self):
        # Retiring an unverified replacement lease must not report success.
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            dest, old, od, new, nd = self._installed(work)
            args = self._args(old, od, new, nd)
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **args)
                txn = upgrade._transaction_root(dest)
                detached = work / "original-empty-transaction"
                original_uninstall = upgrade.uninstall_verified_release
                swapped = []

                def replace_empty_transaction(path, **kwargs):
                    result = original_uninstall(path, **kwargs)
                    txn.rename(detached)
                    txn.mkdir(mode=0o700)
                    swapped.append(True)
                    return result

                with patch.object(upgrade, "uninstall_verified_release",
                                  side_effect=replace_empty_transaction):
                    with self.assertRaisesRegex(
                        install.OfflineInstallError,
                        "update transaction identity changed",
                    ):
                        upgrade.finalize_verified_paper_update(dest, **args)
            self.assertEqual(swapped, [True])
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())
            self.assertTrue(txn.is_dir())
            self.assertTrue(detached.is_dir())

    def test_finalize_rejects_active_executable_rewrite_during_obsolete_disposal(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
                txn = upgrade._transaction_root(dest)
                original_uninstall = upgrade.uninstall_verified_release

                def corrupt_active_after_disposal(path, **kwargs):
                    result = original_uninstall(path, **kwargs)
                    (dest / "Autosport.exe").write_bytes(b"corrupted-after-finalize")
                    return result

                with patch.object(upgrade, "uninstall_verified_release",
                                  side_effect=corrupt_active_after_disposal):
                    with self.assertRaises(install.OfflineInstallError):
                        upgrade.finalize_verified_paper_update(
                            dest, **self._args(old, od, new, nd),
                        )
            self.assertTrue(txn.is_dir())
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"corrupted-after-finalize")

    def test_finalize_rejects_identical_active_directory_swap_during_disposal(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
                txn = upgrade._transaction_root(dest)
                original_uninstall = upgrade.uninstall_verified_release

                def replace_active_after_disposal(path, **kwargs):
                    result = original_uninstall(path, **kwargs)
                    dest.rename(txn / "verified-original")
                    shutil.copytree(txn / "verified-original", dest)
                    return result

                with patch.object(upgrade, "uninstall_verified_release",
                                  side_effect=replace_active_after_disposal):
                    with self.assertRaisesRegex(install.OfflineInstallError,
                                                "active release identity changed"):
                        upgrade.finalize_verified_paper_update(
                            dest, **self._args(old, od, new, nd),
                        )
            self.assertTrue((txn / "verified-original").is_dir())

    def test_finalize_rejects_active_pre_disposal_tamper_preserving_previous(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
                txn = upgrade._transaction_root(dest)
                original_lineage = upgrade._require_forward_update_lineage

                def tamper_after_lineage(**kwargs):
                    result = original_lineage(**kwargs)
                    (dest / "Autosport.exe").write_bytes(b"corrupted-before-disposal")
                    return result

                with patch.object(upgrade, "_require_forward_update_lineage",
                                  side_effect=tamper_after_lineage):
                    with self.assertRaises(install.OfflineInstallError):
                        upgrade.finalize_verified_paper_update(
                            dest, **self._args(old, od, new, nd),
                        )
            self.assertTrue((txn / "previous").is_dir())


    def test_finalize_rejects_active_rewrite_during_transaction_retirement(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
                txn = upgrade._transaction_root(dest)
                original_rmdir = Path.rmdir

                def corrupt_on_retirement(path):
                    result = original_rmdir(path)
                    if Path(path) == txn:
                        (dest / "Autosport.exe").write_bytes(b"corrupted-at-retirement")
                    return result

                with patch.object(Path, "rmdir", new=corrupt_on_retirement):
                    with self.assertRaises(install.OfflineInstallError):
                        upgrade.finalize_verified_paper_update(
                            dest, **self._args(old, od, new, nd),
                        )
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"corrupted-at-retirement")

    def test_finalize_rejects_identical_directory_swap_during_transaction_retirement(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **self._args(old, od, new, nd))
                txn = upgrade._transaction_root(dest)
                original_rmdir = Path.rmdir
                original_directory = dest.parent / "verified-active-before-retirement"

                def replace_on_retirement(path):
                    result = original_rmdir(path)
                    if Path(path) == txn:
                        dest.rename(original_directory)
                        shutil.copytree(original_directory, dest)
                    return result

                with patch.object(Path, "rmdir", new=replace_on_retirement):
                    with self.assertRaisesRegex(
                        install.OfflineInstallError, "active release identity changed",
                    ):
                        upgrade.finalize_verified_paper_update(
                            dest, **self._args(old, od, new, nd),
                        )
            self.assertTrue(original_directory.is_dir())

    def test_finalize_rejects_rejected_current_rewrite_during_transaction_retirement(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            args = self._args(old, od, new, nd)
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **args)
                upgrade.rollback_verified_paper_release(dest, **args)
                txn = upgrade._transaction_root(dest)
                self.assertTrue((txn / "rejected-current").is_dir())
                original_rmdir = Path.rmdir

                def corrupt_on_retirement(path):
                    result = original_rmdir(path)
                    if Path(path) == txn:
                        (dest / "Autosport.exe").write_bytes(b"corrupted-old-at-retirement")
                    return result

                with patch.object(Path, "rmdir", new=corrupt_on_retirement):
                    with self.assertRaises(install.OfflineInstallError):
                        upgrade.finalize_verified_paper_update(dest, **args)
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"corrupted-old-at-retirement")

    def test_finalize_rejects_candidate_rewrite_during_transaction_retirement(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            args = self._args(old, od, new, nd)
            txn = upgrade._transaction_root(dest)
            publish = upgrade._publish_directory_no_replace

            def interrupted_upgrade(source, target):
                if Path(target) == txn / "previous":
                    raise OSError("injected crash before old release move")
                return publish(source, target)

            with self._trusted():
                with patch.object(upgrade, "_publish_directory_no_replace", side_effect=interrupted_upgrade):
                    with self.assertRaisesRegex(OSError, "injected crash"):
                        upgrade.upgrade_verified_paper_release(dest, **args)
                self.assertTrue((txn / "candidate").is_dir())
                self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())
                original_rmdir = Path.rmdir

                def corrupt_on_retirement(path):
                    result = original_rmdir(path)
                    if Path(path) == txn:
                        (dest / "Autosport.exe").write_bytes(b"corrupted-candidate-retirement")
                    return result

                with patch.object(Path, "rmdir", new=corrupt_on_retirement):
                    with self.assertRaises(install.OfflineInstallError):
                        upgrade.finalize_verified_paper_update(dest, **args)
            self.assertEqual((dest / "Autosport.exe").read_bytes(), b"corrupted-candidate-retirement")

    def test_finalize_rejects_deleted_executable_during_transaction_retirement(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            args = self._args(old, od, new, nd)
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **args)
                txn = upgrade._transaction_root(dest)
                original_rmdir = Path.rmdir

                def delete_active_on_retirement(path):
                    result = original_rmdir(path)
                    if Path(path) == txn:
                        (dest / "Autosport.exe").unlink()
                    return result

                with patch.object(Path, "rmdir", new=delete_active_on_retirement):
                    with self.assertRaises(install.OfflineInstallError):
                        upgrade.finalize_verified_paper_update(dest, **args)
            self.assertFalse((dest / "Autosport.exe").exists())


    def test_finalize_retirement_rmdir_failure_never_reports_success(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            args = self._args(old, od, new, nd)
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **args)
                txn = upgrade._transaction_root(dest)
                original_rmdir = Path.rmdir

                def fail_retirement(path):
                    if Path(path) == txn:
                        raise OSError("injected transaction retirement failure")
                    return original_rmdir(path)

                with patch.object(Path, "rmdir", new=fail_retirement):
                    with self.assertRaisesRegex(OSError, "injected transaction retirement failure"):
                        upgrade.finalize_verified_paper_update(dest, **args)
            self.assertTrue(txn.is_dir())
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())


    def test_finalize_empty_lease_after_retirement_failure_is_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            args = self._args(old, od, new, nd)
            with self._trusted():
                upgrade.upgrade_verified_paper_release(dest, **args)
                txn = upgrade._transaction_root(dest)
                original_rmdir = Path.rmdir

                def fail_retirement(path):
                    if Path(path) == txn:
                        raise OSError("injected retirement interruption")
                    return original_rmdir(path)

                with patch.object(Path, "rmdir", new=fail_retirement):
                    with self.assertRaisesRegex(OSError, "retirement interruption"):
                        upgrade.finalize_verified_paper_update(dest, **args)
                self.assertEqual(list(txn.iterdir()), [])
                recovered = upgrade.finalize_verified_paper_update(dest, **args)
            self.assertEqual(recovered["status"], "EMPTY_UPDATE_TRANSACTION_RETIRED")
            self.assertEqual(recovered["source_sha"], self.NEW)
            self.assertFalse(recovered["real_money_execution"])
            self.assertFalse(txn.exists())
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.NEW.encode())

    def test_finalize_empty_lease_after_failed_staging_is_not_false_success(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            with self._trusted():
                result = upgrade.finalize_verified_paper_update(
                    dest, **self._args(old, od, new, nd),
                )
            self.assertEqual(result["status"], "EMPTY_UPDATE_TRANSACTION_RETIRED")
            self.assertEqual(result["source_sha"], self.OLD)
            self.assertFalse(result["real_money_execution"])
            self.assertFalse(txn.exists())
            self.assertEqual((dest / "Autosport.exe").read_bytes(), self.OLD.encode())

    def test_finalize_empty_lease_preserves_unknown_owner_content(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            (txn / "owner.txt").write_bytes(b"preserve")
            with self._trusted():
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "unknown or ambiguous",
                ):
                    upgrade.finalize_verified_paper_update(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertEqual((txn / "owner.txt").read_bytes(), b"preserve")

    def test_finalize_empty_lease_refuses_unverified_active_product(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            (dest / "Autosport.exe").write_bytes(b"untrusted")
            with self._trusted():
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "no verified active release",
                ):
                    upgrade.finalize_verified_paper_update(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertTrue(txn.is_dir())

    def test_finalize_empty_lease_rejects_transaction_identity_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            witness = upgrade._directory_witness
            checks = 0

            def injected_witness(path):
                nonlocal checks
                actual = witness(path)
                if Path(path) == txn:
                    checks += 1
                    if checks == 2:
                        return actual[0], actual[1] + 1
                return actual

            with self._trusted(), patch.object(
                upgrade, "_directory_witness", side_effect=injected_witness,
            ):
                with self.assertRaisesRegex(
                    install.OfflineInstallError, "empty update transaction identity changed",
                ):
                    upgrade.finalize_verified_paper_update(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertEqual(checks, 2)
            self.assertTrue(txn.is_dir())

    def test_finalize_empty_lease_rejects_post_retirement_payload_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            dest, old, od, new, nd = self._installed(Path(temp))
            txn = upgrade._transaction_root(dest)
            txn.mkdir(mode=0o700)
            original_rmdir = Path.rmdir

            def corrupt_on_retirement(path):
                result = original_rmdir(path)
                if Path(path) == txn:
                    (dest / "Autosport.exe").write_bytes(b"corrupted-after-retirement")
                return result

            with self._trusted(), patch.object(
                Path, "rmdir", new=corrupt_on_retirement,
            ):
                with self.assertRaises(install.OfflineInstallError):
                    upgrade.finalize_verified_paper_update(
                        dest, **self._args(old, od, new, nd),
                    )
            self.assertFalse(txn.exists())
            self.assertEqual(
                (dest / "Autosport.exe").read_bytes(), b"corrupted-after-retirement",
            )


# Exact-SHA CI regression: include these real production lifecycle scenarios in the
# canonical paper_install_update_recovery source qualifier's existing test path.
OLD_SHA = "a" * 40
NEW_SHA = "b" * 40


def _package(root: Path, name: str, *, source: str, version: str, schema: int):
    archive = root / name
    build = {
        "product": "Autosport", "version": version, "update_version": version,
        "update_schema": schema, "source_sha": source,
        "real_money_execution": False, "human_tested": False,
        "nvda_verified": False,
    }
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("Autosport-V1/Autosport.exe", source.encode("ascii"))
        writer.writestr("Autosport-V1/BUILD_INFO.json", json.dumps(build))
        writer.writestr("Autosport-V1/examples/demo.txt", "PAPER-only")
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


@pytest.fixture
def release_case(tmp_path):
    old, old_digest = _package(
        tmp_path, "old.zip", source=OLD_SHA, version="1.0.0", schema=1,
    )
    new, new_digest = _package(
        tmp_path, "new.zip", source=NEW_SHA, version="1.1.0", schema=1,
    )
    destination = tmp_path / "Програма Autosport з пробілами"
    workspace = tmp_path / "Приватні дані користувача"
    workspace.mkdir()
    (workspace / "сесія.json").write_bytes(b'{"paper":true,"token":"never-log"}')
    preserved = (workspace / "сесія.json").read_bytes()

    # A fixture-only verifier is not a cryptographic or release qualification.
    def verify_fixture(archive, *, expected_source_sha, **_kwargs):
        return {
            "status": "PASS", "source_sha": expected_source_sha,
            "package_sha256": hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
        }

    with patch.object(
        install.release_package, "verify_windows_package", side_effect=verify_fixture,
    ):
        yield {
            "destination": destination, "workspace": workspace,
            "preserved": preserved, "old": old, "old_digest": old_digest,
            "new": new, "new_digest": new_digest,
        }
    assert (workspace / "сесія.json").read_bytes() == preserved


def _old(case):
    return dict(
        archive=case["old"], expected_source_sha=OLD_SHA,
        expected_package_sha256=case["old_digest"],
    )


def _new(case):
    return dict(
        archive=case["new"], expected_source_sha=NEW_SHA,
        expected_package_sha256=case["new_digest"],
    )


def _transition(case):
    return dict(
        old_archive=case["old"], old_source_sha=OLD_SHA,
        old_package_sha256=case["old_digest"],
        new_archive=case["new"], new_source_sha=NEW_SHA,
        new_package_sha256=case["new_digest"],
    )


def test_clean_install_upgrade_rollback_finalize_uninstall_reinstall(release_case):
    case = release_case
    destination = case["destination"]
    assert install.install_verified_release(
        case["old"], destination, expected_source_sha=OLD_SHA,
        expected_package_sha256=case["old_digest"],
    )["status"] == "INSTALLED"
    assert lifecycle.inspect_installed_release(destination, **_old(case))["status"] == "VERIFIED"

    update = upgrade.upgrade_verified_paper_release(destination, **_transition(case))
    assert update["status"] == "UPDATED_PAPER_ONLY"
    assert update["schema_migrated"] is False
    assert lifecycle.inspect_installed_release(destination, **_new(case))["status"] == "VERIFIED"

    rollback = upgrade.rollback_verified_paper_release(destination, **_transition(case))
    assert rollback["status"] == "ROLLED_BACK_PAPER_ONLY"
    assert lifecycle.inspect_installed_release(destination, **_old(case))["status"] == "VERIFIED"
    assert upgrade.finalize_verified_paper_update(
        destination, **_transition(case),
    )["status"] == "PAPER_UPDATE_FINALIZED"

    assert lifecycle.uninstall_verified_release(destination, **_old(case))["status"] == "UNINSTALLED"
    assert not destination.exists()
    assert install.install_verified_release(
        case["new"], destination, expected_source_sha=NEW_SHA,
        expected_package_sha256=case["new_digest"],
    )["status"] == "INSTALLED"
    assert lifecycle.inspect_installed_release(destination, **_new(case))["status"] == "VERIFIED"
    assert (case["workspace"] / "сесія.json").read_bytes() == case["preserved"]


def test_interrupted_publication_recovers_and_disposes_candidate(release_case):
    case = release_case
    destination = case["destination"]
    install.install_verified_release(
        case["old"], destination, expected_source_sha=OLD_SHA,
        expected_package_sha256=case["old_digest"],
    )
    real_publish = upgrade._publish_directory_no_replace
    transaction = upgrade._transaction_root(destination)

    def simulate_process_abort(source, target):
        if Path(source) == transaction / "candidate" and Path(target) == destination:
            raise KeyboardInterrupt("simulated power loss before publication")
        return real_publish(source, target)

    with patch.object(upgrade, "_publish_directory_no_replace", side_effect=simulate_process_abort):
        with pytest.raises(KeyboardInterrupt, match="simulated power loss"):
            upgrade.upgrade_verified_paper_release(destination, **_transition(case))

    assert not destination.exists()
    assert (transaction / "previous").is_dir()
    assert (transaction / "candidate").is_dir()
    assert upgrade.recover_update_gap(
        destination, old_archive=case["old"], old_source_sha=OLD_SHA,
        old_package_sha256=case["old_digest"],
    )["status"] == "OLD_RELEASE_RECOVERED"
    assert lifecycle.inspect_installed_release(destination, **_old(case))["status"] == "VERIFIED"
    with pytest.raises(install.OfflineInstallError, match="previous update transaction"):
        upgrade.upgrade_verified_paper_release(destination, **_transition(case))
    assert upgrade.finalize_verified_paper_update(
        destination, **_transition(case),
    )["status"] == "PAPER_UPDATE_FINALIZED"
    assert not transaction.exists()
    assert upgrade.upgrade_verified_paper_release(
        destination, **_transition(case),
    )["status"] == "UPDATED_PAPER_ONLY"
    assert (case["workspace"] / "сесія.json").read_bytes() == case["preserved"]


def test_schema_change_refused_without_migration_or_transaction(release_case, tmp_path):
    case = release_case
    destination = case["destination"]
    install.install_verified_release(
        case["old"], destination, expected_source_sha=OLD_SHA,
        expected_package_sha256=case["old_digest"],
    )
    future, digest = _package(
        tmp_path, "future.zip", source=NEW_SHA, version="2.0.0", schema=2,
    )
    args = _transition(case)
    args.update(new_archive=future, new_package_sha256=digest)
    with pytest.raises(install.OfflineInstallError, match="unsupported schema-changing"):
        upgrade.upgrade_verified_paper_release(destination, **args)
    assert not upgrade._transaction_root(destination).exists()
    assert lifecycle.inspect_installed_release(destination, **_old(case))["status"] == "VERIFIED"
    assert (case["workspace"] / "сесія.json").read_bytes() == case["preserved"]


def test_unowned_install_file_blocks_uninstall_without_deleting_owner_data(release_case):
    case = release_case
    destination = case["destination"]
    install.install_verified_release(
        case["old"], destination, expected_source_sha=OLD_SHA,
        expected_package_sha256=case["old_digest"],
    )
    owner_file = destination / "мій файл.txt"
    owner_file.write_text("Зберегти", encoding="utf-8")
    with pytest.raises(install.OfflineInstallError):
        lifecycle.uninstall_verified_release(destination, **_old(case))
    assert owner_file.read_text(encoding="utf-8") == "Зберегти"
    assert (destination / "Autosport.exe").read_bytes() == OLD_SHA.encode("ascii")
    assert (case["workspace"] / "сесія.json").read_bytes() == case["preserved"]


if __name__ == "__main__":
    unittest.main()
