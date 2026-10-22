"""Offline, separately SHA-pinned PAPER release update and rollback.

No bookmaker/funding interface and no production-signature claim. The entire
installed product must match the old immutable archive; user-owned files and
schema-changing updates are refused instead of silently migrated.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import stat
import zipfile
from pathlib import Path, PureWindowsPath

from . import release_package
from .offline_release_install import (
    OfflineInstallError, _publish_directory_no_replace, install_verified_release,
)
from .offline_release_lifecycle import (
    _inspection_root, _trusted_archive_inventory, inspect_installed_release,
    uninstall_verified_release,
)
from .windows_update_admission import _schema, _version


def _transaction_root(destination: Path) -> Path:
    if not destination.is_absolute() or destination.parent == destination:
        raise OfflineInstallError("update destination must be a concrete absolute path")
    # One installed directory must have one transaction namespace. A spelling
    # such as /install/other/../product otherwise points to the same product
    # while generating a distinct transaction digest, bypassing the single
    # pending-update guard during upgrade, rollback and crash recovery.
    if ".." in destination.parts:
        raise OfflineInstallError("update destination contains parent traversal")
    # Windows product paths are case-insensitive. Without a canonical key,
    # C:\\Apps\\Autosport and c:\\apps\\AUTOSPORT address the same install
    # but receive different transaction directories, bypassing the pending-
    # update fence. Preserve POSIX case sensitivity.
    spelling = os.fspath(destination)
    if isinstance(destination, PureWindowsPath):
        # Win32 device/extended-length aliases (\\?\C:\... and \\.\C:\...)
        # can address the same product as C:\... but hash to a different
        # pending-update namespace. Refuse them rather than permit bypass.
        if destination.drive.startswith(("\\\\?\\", "\\\\.\\")):
            raise OfflineInstallError("Windows device namespace update path is unsafe")
        # Ordinary Win32 file APIs resolve trailing dots/spaces to the same
        # component. Those aliases must not obtain a second sibling pending-
        # update lease, even when the final installed directory is unchanged.
        if any(part.endswith((".", " ")) for part in destination.parts[1:]):
            raise OfflineInstallError("Windows trailing-dot-or-space update path is unsafe")
        spelling = ntpath.normcase(spelling)
    name = hashlib.sha256(os.fsencode(spelling)).hexdigest()[:24]
    return destination.parent / (".autosport-update-" + name)


def _directory_witness(root: Path) -> tuple[int, int]:
    """Bind an inspected directory to its actual, non-symlink filesystem object."""
    info = root.lstat()
    # On Windows a junction/reparse directory may still report S_IFDIR, so
    # S_ISDIR alone does not certify the object as a local real directory.
    if (
        not stat.S_ISDIR(info.st_mode)
        or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    ):
        raise OfflineInstallError("verified release directory was replaced by a redirect")
    # Some Windows/network filesystems cannot supply a stable directory inode.
    # A zero/invalid inode is not a valid identity witness for the exact release
    # that will be moved during upgrade/rollback; fail closed instead of
    # falsely proving continuity from two indistinguishable (dev, 0) tuples.
    if (
        type(info.st_dev) is not int or info.st_dev < 0
        or type(info.st_ino) is not int or info.st_ino <= 0
    ):
        raise OfflineInstallError("verified release directory identity unavailable")
    return info.st_dev, info.st_ino


def _identity(archive: Path, source_sha: str, digest: str) -> tuple[tuple[int, int, int], int]:
    trusted = _trusted_archive_inventory(
        archive, expected_source_sha=source_sha,
        expected_package_sha256=digest,
    )
    if "BUILD_INFO.json" not in trusted:
        raise OfflineInstallError("update archive has no verified build identity")
    with zipfile.ZipFile(archive) as stream:
        info = stream.getinfo("Autosport-V1/BUILD_INFO.json")
        if info.file_size > 8192:
            raise OfflineInstallError("update BUILD_INFO exceeds size bound")
        payload = stream.read(info)
    if hashlib.sha256(payload).hexdigest() != trusted["BUILD_INFO.json"]:
        raise OfflineInstallError("update build identity changed during inspection")
    try:
        info = release_package._decode_json_object(payload, "update BUILD_INFO")
        if (
            info.get("product") != "Autosport" or info.get("source_sha") != source_sha
            or any(info.get(key) is not False for key in (
                "real_money_execution", "human_tested", "nvda_verified"
            )) or info.get("version") != info.get("update_version")
        ):
            raise ValueError("untrusted product update identity")
        return _version(info.get("update_version")), _schema(info.get("update_schema"))
    except (ValueError, TypeError) as exc:
        raise OfflineInstallError("canonical update version/schema required") from exc


def upgrade_verified_paper_release(
    destination: str | Path, *,
    old_archive: str | Path, old_source_sha: str, old_package_sha256: str,
    new_archive: str | Path, new_source_sha: str, new_package_sha256: str,
) -> dict[str, object]:
    """Replace exact PAPER product with strictly newer, same-schema release.

    The previous verified product stays in a private sibling transaction
    directory for explicit rollback, never silently removed. A crash after
    moving the old release is recoverable via recover_update_gap.
    """
    # Reject lexical destination aliases before installed receipt/archive reads.
    _transaction_root(Path(destination))
    root = _inspection_root(destination)
    incumbent_witness = _directory_witness(root)
    old_archive, new_archive = Path(old_archive), Path(new_archive)
    inspect_installed_release(
        root, archive=old_archive,
        expected_source_sha=old_source_sha,
        expected_package_sha256=old_package_sha256,
    )
    if _directory_witness(root) != incumbent_witness:
        raise OfflineInstallError("installed directory identity changed during initial verification")
    old_version, old_schema = _identity(old_archive, old_source_sha, old_package_sha256)
    new_version, new_schema = _identity(new_archive, new_source_sha, new_package_sha256)
    if (
        new_version <= old_version or new_schema != old_schema
        or new_package_sha256 == old_package_sha256
    ):
        raise OfflineInstallError("nonprogressing or unsupported schema-changing update")
    txn = _transaction_root(root)
    if txn.exists() or txn.is_symlink():
        raise OfflineInstallError("previous update transaction requires disposition")
    txn.mkdir(mode=0o700)
    candidate = txn / "candidate"
    previous = txn / "previous"
    try:
        install_verified_release(
            new_archive, candidate, expected_source_sha=new_source_sha,
            expected_package_sha256=new_package_sha256,
        )
        inspect_installed_release(
            candidate, archive=new_archive,
            expected_source_sha=new_source_sha,
            expected_package_sha256=new_package_sha256,
        )
        # A matching file inventory is not enough if another actor replaces
        # the entire staged directory after inspection. Bind its filesystem
        # object, as we already do for the installed incumbent.
        candidate_witness = _directory_witness(candidate)
        inspect_installed_release(
            root, archive=old_archive,
            expected_source_sha=old_source_sha,
            expected_package_sha256=old_package_sha256,
        )
    except Exception:
        # Only an entirely empty transaction may be silently cleaned.
        # Incomplete candidate material stays quarantined for diagnosis.
        try:
            txn.rmdir()
        except OSError:
            pass
        raise
    original = root.lstat()
    if (not stat.S_ISDIR(original.st_mode)
            or (original.st_dev, original.st_ino) != incumbent_witness):
        raise OfflineInstallError("installed directory identity changed before update commit")
    if _directory_witness(candidate) != candidate_witness:
        raise OfflineInstallError("staged release identity changed before update commit")
    # Atomic no-replace is required here too: on POSIX rename() can silently
    # replace an empty quarantine directory created by a concurrent worker.
    _publish_directory_no_replace(root, previous)
    if (previous.stat().st_dev, previous.stat().st_ino) != (
        original.st_dev, original.st_ino
    ):
        raise OfflineInstallError("installed identity changed during update")
    try:
        # The old release has moved, so re-check the exact vetted candidate
        # before publication; restore previous on a witnessed replacement.
        if _directory_witness(candidate) != candidate_witness:
            raise OfflineInstallError("staged release identity changed before publication")
        _publish_directory_no_replace(candidate, root)
    except Exception:
        if not root.exists() and previous.is_dir():
            _publish_directory_no_replace(previous, root)
        raise
    if _directory_witness(root) != candidate_witness:
        # Never report success for bytes published from an unverified
        # replacement. Preserve previous for explicit recovery/disposition.
        raise OfflineInstallError("published release identity changed during update")
    return {
        "status": "UPDATED_PAPER_ONLY", "old_source_sha": old_source_sha,
        "source_sha": new_source_sha, "package_sha256": new_package_sha256,
        "previous_release_path": str(previous),
        "schema_migrated": False, "real_money_execution": False,
    }


def recover_update_gap(
    destination: str | Path, *,
    old_archive: str | Path, old_source_sha: str, old_package_sha256: str,
) -> dict[str, object]:
    """Restore exact previous release when a crash left no installed directory."""
    root = Path(destination)
    txn = _transaction_root(root)
    if root.exists() or root.is_symlink():
        raise OfflineInstallError("recovery cannot replace an occupied update destination")
    previous = _inspection_root(txn / "previous")
    # Recovery is also a destructive directory publication. Bind the exact
    # previously inspected filesystem object, not just reproducible bytes:
    # another process could replace it with an identical copy after the
    # trusted installed-release read and before it is moved into place.
    previous_witness = _directory_witness(previous)
    inspect_installed_release(
        previous, archive=old_archive, expected_source_sha=old_source_sha,
        expected_package_sha256=old_package_sha256,
    )
    if _directory_witness(previous) != previous_witness:
        raise OfflineInstallError("previous release identity changed during recovery")
    _publish_directory_no_replace(previous, root)
    if _directory_witness(root) != previous_witness:
        raise OfflineInstallError("restored release identity changed during recovery")
    # Moving the sole `previous` release back leaves an empty transaction
    # directory. Keeping that empty lease would reject every future upgrade
    # as "previous update transaction requires disposition", even though the
    # old release was successfully recovered. Only retire an empty directory;
    # retained candidate/unknown artifacts must remain for explicit review.
    try:
        txn.rmdir()
    except OSError:
        if not any(txn.iterdir()):
            raise OfflineInstallError("empty recovered transaction could not be retired")
    return {"status": "OLD_RELEASE_RECOVERED", "source_sha": old_source_sha,
            "real_money_execution": False}


def _require_forward_update_lineage(
    *, old_archive: Path, old_source_sha: str, old_package_sha256: str,
    new_archive: Path, new_source_sha: str, new_package_sha256: str,
) -> None:
    """Authenticate the original upgrade order before a destructive disposition.

    A recovered transaction directory does not prove that its previous product
    predates the active one. Both independently pinned archives must describe
    a strictly forward, same-schema PAPER upgrade.
    """
    old_version, old_schema = _identity(
        Path(old_archive), old_source_sha, old_package_sha256,
    )
    new_version, new_schema = _identity(
        Path(new_archive), new_source_sha, new_package_sha256,
    )
    if (
        new_version <= old_version
        or new_schema != old_schema
        or new_package_sha256 == old_package_sha256
    ):
        raise OfflineInstallError("update transaction has no forward same-schema lineage")


def rollback_verified_paper_release(
    destination: str | Path, *,
    old_archive: str | Path, old_source_sha: str, old_package_sha256: str,
    new_archive: str | Path, new_source_sha: str, new_package_sha256: str,
) -> dict[str, object]:
    """Rollback only verified product bytes; keep new release quarantined."""
    txn = _transaction_root(Path(destination))
    root = _inspection_root(destination)
    previous = _inspection_root(txn / "previous")
    previous_witness = _directory_witness(previous)
    incumbent_witness = _directory_witness(root)
    rejected = txn / "rejected-current"
    if rejected.exists() or rejected.is_symlink():
        raise OfflineInstallError("rollback candidate already exists")
    inspect_installed_release(
        previous, archive=old_archive, expected_source_sha=old_source_sha,
        expected_package_sha256=old_package_sha256,
    )
    inspect_installed_release(
        root, archive=new_archive, expected_source_sha=new_source_sha,
        expected_package_sha256=new_package_sha256,
    )
    _require_forward_update_lineage(
        old_archive=Path(old_archive), old_source_sha=old_source_sha,
        old_package_sha256=old_package_sha256,
        new_archive=Path(new_archive), new_source_sha=new_source_sha,
        new_package_sha256=new_package_sha256,
    )
    original = root.lstat()
    if (not stat.S_ISDIR(original.st_mode)
            or (original.st_dev, original.st_ino) != incumbent_witness
            or _directory_witness(previous) != previous_witness):
        raise OfflineInstallError("release identity changed before rollback commit")
    # Do not allow a concurrent newly-created quarantine directory to be
    # replaced by the installed product on POSIX.
    _publish_directory_no_replace(root, rejected)
    if (rejected.stat().st_dev, rejected.stat().st_ino) != (
        original.st_dev, original.st_ino
    ):
        raise OfflineInstallError("installed identity changed during rollback")
    try:
        _publish_directory_no_replace(previous, root)
    except Exception:
        if not root.exists() and rejected.is_dir():
            _publish_directory_no_replace(rejected, root)
        raise
    if _directory_witness(root) != previous_witness:
        raise OfflineInstallError("restored release identity changed during rollback")
    return {"status": "ROLLED_BACK_PAPER_ONLY", "source_sha": old_source_sha,
            "rejected_release_path": str(rejected), "real_money_execution": False}




def finalize_verified_paper_update(
    destination: str | Path, *,
    old_archive: str | Path, old_source_sha: str, old_package_sha256: str,
    new_archive: str | Path, new_source_sha: str, new_package_sha256: str,
) -> dict[str, object]:
    """Dispose only a separately verified obsolete product, never user files.

    An exact active product and one exact previous/rejected product are
    independently reverified. Unknown transaction contents stop disposal.
    """
    txn_path = _transaction_root(Path(destination))
    root = _inspection_root(destination)
    txn = _inspection_root(txn_path)
    entries = {item.name for item in txn.iterdir()}
    if entries == {"previous"}:
        active = (new_archive, new_source_sha, new_package_sha256)
        obsolete = txn / "previous"
        discarded = (old_archive, old_source_sha, old_package_sha256)
    elif entries == {"rejected-current"} or entries == {"candidate"}:
        active = (old_archive, old_source_sha, old_package_sha256)
        obsolete = txn / next(iter(entries))
        discarded = (new_archive, new_source_sha, new_package_sha256)
    else:
        raise OfflineInstallError("unknown or ambiguous update transaction state")
    inspect_installed_release(
        root, archive=active[0],
        expected_source_sha=active[1], expected_package_sha256=active[2],
    )
    inspect_installed_release(
        obsolete, archive=discarded[0],
        expected_source_sha=discarded[1], expected_package_sha256=discarded[2],
    )
    _require_forward_update_lineage(
        old_archive=Path(old_archive), old_source_sha=old_source_sha,
        old_package_sha256=old_package_sha256,
        new_archive=Path(new_archive), new_source_sha=new_source_sha,
        new_package_sha256=new_package_sha256,
    )
    uninstall_verified_release(
        obsolete, archive=discarded[0],
        expected_source_sha=discarded[1], expected_package_sha256=discarded[2],
    )
    txn.rmdir()
    return {"status": "PAPER_UPDATE_FINALIZED", "source_sha": active[1],
            "real_money_execution": False}


def main(argv: list[str] | None = None) -> int:
    """Operator-only PAPER update tooling; failures never echo private paths."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Independently pinned PAPER-only release transaction")
    parser.add_argument("action", choices=("upgrade-release", "rollback-release", "recover-update-release", "finalize-update-release"))
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--old-archive", type=Path, required=True)
    parser.add_argument("--old-source-sha", required=True)
    parser.add_argument("--old-package-sha256", required=True)
    parser.add_argument("--new-archive", type=Path)
    parser.add_argument("--new-source-sha")
    parser.add_argument("--new-package-sha256")
    args = parser.parse_args(argv)
    old = {
        "old_archive": args.old_archive,
        "old_source_sha": args.old_source_sha,
        "old_package_sha256": args.old_package_sha256,
    }
    try:
        if args.action == "recover-update-release":
            report = recover_update_gap(args.destination, **old)
        else:
            if not all((args.new_archive, args.new_source_sha, args.new_package_sha256)):
                raise OfflineInstallError("new release identity is required")
            new = {
                "new_archive": args.new_archive,
                "new_source_sha": args.new_source_sha,
                "new_package_sha256": args.new_package_sha256,
            }
            operation = (
                upgrade_verified_paper_release if args.action == "upgrade-release"
                else rollback_verified_paper_release if args.action == "rollback-release"
                else finalize_verified_paper_update
            )
            report = operation(args.destination, **old, **new)
    except (OfflineInstallError, OSError, ValueError, TypeError, RuntimeError, zipfile.BadZipFile):
        print("release_update=FAIL_CLOSED reason=unsafe_or_unverified_release")
        return 3
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
