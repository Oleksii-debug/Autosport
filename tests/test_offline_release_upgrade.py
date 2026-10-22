"""Plan 6 §6А: pinned same-schema PAPER upgrades, rollback and recovery."""
from __future__ import annotations

import hashlib
import json
import stat
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

from autosport import offline_release_install as install
from autosport import offline_release_upgrade as upgrade


class OfflineReleaseUpgradeTests(unittest.TestCase):
    OLD = "a" * 40
    NEW = "b" * 40

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


if __name__ == "__main__":
    unittest.main()
