"""Offline, separately SHA-pinned PAPER release update and rollback.

No bookmaker/funding interface and no production-signature claim. The entire
installed product must match the old immutable archive; user-owned files and
schema-changing updates are refused instead of silently migrated.
"""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

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
    name = hashlib.sha256(os.fsencode(str(destination))).hexdigest()[:24]
    return destination.parent / (".autosport-update-" + name)


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
    root = _inspection_root(destination)
    old_archive, new_archive = Path(old_archive), Path(new_archive)
    inspect_installed_release(
        root, archive=old_archive,
        expected_source_sha=old_source_sha,
        expected_package_sha256=old_package_sha256,
    )
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
    original = root.stat()
    # Atomic no-replace is required here too: on POSIX rename() can silently
    # replace an empty quarantine directory created by a concurrent worker.
    _publish_directory_no_replace(root, previous)
    if (previous.stat().st_dev, previous.stat().st_ino) != (
        original.st_dev, original.st_ino
    ):
        raise OfflineInstallError("installed identity changed during update")
    try:
        _publish_directory_no_replace(candidate, root)
    except Exception:
        if not root.exists() and previous.is_dir():
            _publish_directory_no_replace(previous, root)
        raise
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
    if root.exists() or root.is_symlink():
        raise OfflineInstallError("recovery cannot replace an occupied update destination")
    txn = _transaction_root(root)
    previous = _inspection_root(txn / "previous")
    inspect_installed_release(
        previous, archive=old_archive, expected_source_sha=old_source_sha,
        expected_package_sha256=old_package_sha256,
    )
    _publish_directory_no_replace(previous, root)
    return {"status": "OLD_RELEASE_RECOVERED", "source_sha": old_source_sha,
            "real_money_execution": False}


def rollback_verified_paper_release(
    destination: str | Path, *,
    old_archive: str | Path, old_source_sha: str, old_package_sha256: str,
    new_archive: str | Path, new_source_sha: str, new_package_sha256: str,
) -> dict[str, object]:
    """Rollback only verified product bytes; keep new release quarantined."""
    root = _inspection_root(destination)
    txn = _transaction_root(root)
    previous = _inspection_root(txn / "previous")
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
    original = root.stat()
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
    root = _inspection_root(destination)
    txn = _inspection_root(_transaction_root(root))
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
