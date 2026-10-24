"""Recover only an independently pinned PAPER release interrupted mid-uninstall.

The recovery source is the user's original, fully authenticated release archive,
not the mutable quarantine or a user-supplied receipt. Nothing is overwritten;
private/unrecognized files make recovery fail closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zipfile
from pathlib import Path

from . import release_package
from .offline_release_install import (
    OfflineInstallError, _RECEIPT, _publish_directory_no_replace, _safe_root,
    _is_redirected_install_path,
)
from .offline_release_lifecycle import (
    _inspection_root, _trusted_archive_inventory, inspect_installed_release,
)
from .offline_release_upgrade import _directory_witness

_QUARANTINE_NAME = re.compile(r"\.autosport-uninstall-[A-Za-z0-9_-]{6,32}\Z")
_MAX_ENTRIES = 4096


def recover_interrupted_uninstall(
    quarantine: str | Path,
    destination: str | Path,
    *,
    archive: str | Path,
    expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, object]:
    """Restore missing release-owned files after interrupted removal, then publish.

    Quarantine must be the canonical .autosport-uninstall-*/verified-product
    sibling of a currently absent destination. The original authenticated ZIP
    alone defines what may be restored. Unknown content is never deleted.
    """
    destination = _safe_root(Path(destination))
    quarantine = Path(quarantine)
    if (
        not quarantine.is_absolute()
        or quarantine.name != "verified-product"
        or not _QUARANTINE_NAME.fullmatch(quarantine.parent.name)
        or quarantine.parent.parent != destination.parent
        or not quarantine.is_dir()
    ):
        raise OfflineInstallError("uninstall recovery quarantine is not canonical")
    _inspection_root(quarantine)
    # Bind the original quarantine *before* archive validation or the file
    # census. A same-byte replacement during either read must not become a
    # newly trusted object and receive restored files or publication rights.
    quarantine_identity = _directory_witness(quarantine)
    if type(expected_source_sha) is not str or not re.fullmatch(
        r"[a-f0-9]{40}", expected_source_sha
    ):
        raise OfflineInstallError("uninstall recovery requires exact source identity")
    if type(expected_package_sha256) is not str or not re.fullmatch(
        r"[a-f0-9]{64}", expected_package_sha256
    ):
        raise OfflineInstallError("uninstall recovery requires exact archive identity")

    trusted = _trusted_archive_inventory(
        archive,
        expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    if _directory_witness(quarantine) != quarantine_identity:
        raise OfflineInstallError("quarantine identity changed during archive inventory")
    expected_receipt = {
        "schema_version": 1,
        "product": "Autosport",
        "source_sha": expected_source_sha,
        "package_sha256": expected_package_sha256,
        "files": {name: trusted[name] for name in sorted(trusted)},
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    receipt_path = quarantine / _RECEIPT
    if receipt_path.exists() or receipt_path.is_symlink():
        if _is_redirected_install_path(receipt_path):
            raise OfflineInstallError("interrupted release receipt is redirected")
        try:
            receipt_mode = receipt_path.lstat().st_mode
            if (_is_redirected_install_path(receipt_path)
                    or not stat.S_ISREG(receipt_mode)
                    or receipt_path.stat().st_size > 1024 * 1024):
                raise OfflineInstallError("interrupted release receipt is unsafe")
            actual_receipt = release_package._decode_json_object(
                receipt_path.read_bytes(), "interrupted installed-release receipt"
            )
            if actual_receipt != expected_receipt:
                raise OfflineInstallError("interrupted receipt cannot extend release authority")
        except (ValueError, OSError) as exc:
            raise OfflineInstallError("interrupted receipt is invalid") from exc

    expected_dirs = {""}
    for name in trusted:
        parts = name.split("/")
        for i in range(1, len(parts)):
            expected_dirs.add("/".join(parts[:i]))
    discovered: set[str] = set()
    count = 0
    for parent, dirs, names in os.walk(quarantine, topdown=True, followlinks=False):
        prefix_path = Path(parent).relative_to(quarantine)
        prefix = "" if prefix_path == Path(".") else prefix_path.as_posix()
        if prefix not in expected_dirs or _is_redirected_install_path(Path(parent)):
            raise OfflineInstallError("unowned or redirected quarantine directory")
        for dirname in dirs:
            count += 1
            child = Path(parent) / dirname
            rel = "/".join(s for s in (prefix, dirname) if s)
            if (
                count > 2 * _MAX_ENTRIES or rel not in expected_dirs
                or _is_redirected_install_path(child)
                or not stat.S_ISDIR(child.lstat().st_mode)
            ):
                raise OfflineInstallError("unexpected quarantine subdirectory")
        for basename in names:
            count += 1
            child = Path(parent) / basename
            rel = "/".join(s for s in (prefix, basename) if s)
            if count > 2 * _MAX_ENTRIES or (
                rel not in trusted and rel != _RECEIPT
            ) or _is_redirected_install_path(child) or not stat.S_ISREG(child.lstat().st_mode):
                raise OfflineInstallError("unknown or unsafe quarantine file")
            discovered.add(rel)
            if rel != _RECEIPT:
                with child.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != trusted[rel]:
                    raise OfflineInstallError("mutated quarantine file")

    # The census must describe the same filesystem object as the entry
    # witness, before any missing file or receipt is written.
    if _directory_witness(quarantine) != quarantine_identity:
        raise OfflineInstallError("quarantine identity changed during file census")
    # Verify the entire quarantine census before writing even one byte.
    missing = set(trusted) - discovered
    with zipfile.ZipFile(archive) as original:
        for name in sorted(missing):
            if _directory_witness(quarantine) != quarantine_identity:
                raise OfflineInstallError("quarantine identity changed during file restoration")
            member = quarantine.joinpath(*name.split("/"))
            member.parent.mkdir(parents=True, exist_ok=True)
            ancestor = member.parent
            while ancestor != quarantine:
                if (_is_redirected_install_path(ancestor)
                        or not stat.S_ISDIR(ancestor.lstat().st_mode)):
                    raise OfflineInstallError("quarantine ancestor is not a real directory")
                ancestor = ancestor.parent
            with original.open("Autosport-V1/" + name) as reader, member.open("xb") as writer:
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 1024 * 1024 * 1024:
                        raise OfflineInstallError("uninstall recovery member exceeds bound")
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if digest.hexdigest() != trusted[name]:
                raise OfflineInstallError("original recovery member hash changed")
            if _directory_witness(quarantine) != quarantine_identity:
                raise OfflineInstallError("quarantine identity changed during file restoration")
    if _directory_witness(quarantine) != quarantine_identity:
        raise OfflineInstallError("quarantine identity changed before receipt restoration")
    if _RECEIPT not in discovered:
        with receipt_path.open("x", encoding="utf-8") as writer:
            json.dump(expected_receipt, writer, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            writer.write("\n")
            writer.flush()
            os.fsync(writer.fileno())
    # The quarantine is the exact previously inspected filesystem object.
    # Same-byte replacement after inspection must not be published as that
    # object: a copied directory is not a valid continuation of the recovery.
    if _directory_witness(quarantine) != quarantine_identity:
        raise OfflineInstallError("quarantine identity changed before trusted inspection")
    proof = inspect_installed_release(
        quarantine,
        archive=archive,
        expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    if _directory_witness(quarantine) != quarantine_identity:
        raise OfflineInstallError("quarantine identity changed during recovery inspection")
    if destination.exists() or destination.is_symlink():
        raise OfflineInstallError("uninstall recovery destination was concurrently created")
    # An in-place rewrite after the previous inspection preserves directory
    # identity. Recheck both the exact files and their directory witness
    # immediately before the no-replace publication.
    inspect_installed_release(
        quarantine, archive=archive,
        expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    if _directory_witness(quarantine) != quarantine_identity:
        raise OfflineInstallError("quarantine identity changed before recovery publication")
    _publish_directory_no_replace(quarantine, destination)
    if _directory_witness(destination) != quarantine_identity:
        raise OfflineInstallError("published quarantine identity changed during recovery")
    # The payload is inspected before its atomic move; an in-place rewrite
    # during publication keeps the directory inode unchanged. Verify the
    # *published* installed bytes before reporting recovered success or
    # retiring the staging directory. Never trust a pre-move receipt alone.
    proof = inspect_installed_release(
        destination,
        archive=archive,
        expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    if _directory_witness(destination) != quarantine_identity:
        raise OfflineInstallError("published quarantine identity changed during readback")
    try:
        quarantine.parent.rmdir()
    except OSError:
        # Never recursively clean another actor's unrelated data.
        pass
    # Retiring the quarantine is a final filesystem mutation. An in-place
    # executable rewrite leaves its directory inode unchanged, while a copied
    # replacement can preserve all bytes but change the directory identity.
    if _directory_witness(destination) != quarantine_identity:
        raise OfflineInstallError("recovered release identity changed during cleanup")
    proof = inspect_installed_release(
        destination, archive=archive,
        expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    if _directory_witness(destination) != quarantine_identity:
        raise OfflineInstallError("recovered release identity changed after cleanup")
    return {**proof, "status": "UNINSTALL_RECOVERED", "restored_files": len(missing)}


def main(argv: list[str] | None = None) -> int:
    """Shipped Autosport-Data.exe recovery command; never echo untrusted paths."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Restore interrupted removal of exact PAPER release bytes"
    )
    parser.add_argument("--quarantine", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--package-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        report = recover_interrupted_uninstall(
            args.quarantine, args.destination, archive=args.archive,
            expected_source_sha=args.source_sha,
            expected_package_sha256=args.package_sha256,
        )
    except (OfflineInstallError, OSError, ValueError, TypeError, RuntimeError, zipfile.BadZipFile):
        print("uninstall_recovery=FAIL_CLOSED reason=unsafe_or_unverified_release")
        return 3
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
