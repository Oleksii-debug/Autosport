"""Fail-closed, archive-pinned uninstall of an unchanged offline PAPER release.

No workspace, unrecognized file, mutated installed member, or incumbent target
is ever deliberately removed. Update/migration/rollback are separate Plan 6
requirements; this operation grants no LIVE bookmaker capability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path

from . import offline_release_install as installation
from . import release_package


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in items:
        if key in value:
            raise installation.OfflineInstallError("duplicate receipt key")
        value[key] = item
    return value


def _existing_product_root(root: Path) -> Path:
    if not root.is_absolute() or root == root.parent:
        raise installation.OfflineInstallError("uninstall destination must be absolute")
    ancestor = root
    while True:
        if ancestor.is_symlink():
            raise installation.OfflineInstallError("uninstall path traverses a symlink")
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    try:
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise installation.OfflineInstallError("installed root is not a directory")
    except OSError as exc:
        raise installation.OfflineInstallError("installed root is unavailable") from exc
    return root


def _installed_members(root: Path, *, expected_source_sha: str, package_sha256: str, trusted_files: dict[str, str]) -> int:
    root = _existing_product_root(root)
    receipt_path = root / installation._RECEIPT
    try:
        if not stat.S_ISREG(receipt_path.lstat().st_mode):
            raise installation.OfflineInstallError("untrusted installed receipt")
        if receipt_path.stat().st_size > 1024 * 1024:
            raise installation.OfflineInstallError("oversized installed receipt")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise installation.OfflineInstallError("invalid installed receipt") from exc
    if (
        type(receipt) is not dict
        or set(receipt) != {
            "schema_version", "product", "source_sha", "package_sha256", "files",
            "real_money_execution", "human_tested", "nvda_verified",
        }
        or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1
        or receipt["product"] != "Autosport"
        or receipt["source_sha"] != expected_source_sha
        or receipt["package_sha256"] != package_sha256
        or receipt["real_money_execution"] is not False
        or receipt["human_tested"] is not False
        or receipt["nvda_verified"] is not False
    ):
        raise installation.OfflineInstallError("receipt cannot authorize this uninstall")
    files = receipt["files"]
    if type(files) is not dict or not 2 <= len(files) <= installation._MAX_FILES:
        raise installation.OfflineInstallError("invalid installed file inventory")
    if files != trusted_files:
        raise installation.OfflineInstallError("installed receipt differs from verified archive inventory")
    if "Autosport.exe" not in files or "BUILD_INFO.json" not in files:
        raise installation.OfflineInstallError("missing installed product identity")

    expected_files = {installation._RECEIPT}
    expected_dirs: set[str] = set()
    total = 0
    for name, digest in files.items():
        if type(name) is not str or type(digest) is not str:
            raise installation.OfflineInstallError("invalid installed member")
        parts = name.split("/")
        if (
            name == installation._RECEIPT or not parts
            or any(part in {"", ".", ".."} or "\\" in part for part in parts)
            or name.startswith("/") or ":" in name or "\x00" in name
        ):
            raise installation.OfflineInstallError("unsafe installed member")
        installation._canonical_sha(digest, field="receipt member digest", length=64)
        expected_files.add(name)
        expected_dirs.update("/".join(parts[:index]) for index in range(1, len(parts)))
        path = root.joinpath(*parts)
        try:
            item_stat = path.lstat()
            if not stat.S_ISREG(item_stat.st_mode) or item_stat.st_nlink != 1:
                raise installation.OfflineInstallError("installed member is not an ordinary owned file")
            total += item_stat.st_size
            if total > installation._MAX_TOTAL_BYTES:
                raise installation.OfflineInstallError("installed tree exceeds size bound")
            if installation._sha_file(path) != digest:
                raise installation.OfflineInstallError("installed member differs from pinned receipt")
        except OSError as exc:
            raise installation.OfflineInstallError("installed member is unavailable") from exc

    actual_files: set[str] = set()
    actual_dirs: set[str] = set()
    for directory, dirs, members in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        for entry in dirs:
            path = base / entry
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise installation.OfflineInstallError("installed tree contains unsafe directory")
            actual_dirs.add(path.relative_to(root).as_posix())
        for entry in members:
            path = base / entry
            if not stat.S_ISREG(path.lstat().st_mode):
                raise installation.OfflineInstallError("installed tree contains unsafe file")
            actual_files.add(path.relative_to(root).as_posix())
    if actual_files != expected_files or actual_dirs != expected_dirs:
        raise installation.OfflineInstallError("unrecognized data in installed tree; preserve it")
    return len(files)


def _remove_pinned_inventory(root: Path, trusted_files: dict[str, str]) -> None:
    """Remove only named, currently regular release files; never recursively delete.

    A late unowned file prevents the final rmdir and remains in quarantine.
    Partial I/O failure likewise leaves the quarantine for explicit recovery.
    """
    directories = {
        "/".join(name.split("/")[:depth])
        for name in trusted_files
        for depth in range(1, len(name.split("/")))
    }
    for name in sorted(trusted_files):
        member = root.joinpath(*name.split("/"))
        parent = member.parent
        while parent != root:
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise installation.OfflineInstallError("installed parent changed during uninstall")
            parent = parent.parent
        file_stat = member.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise installation.OfflineInstallError("installed file changed during uninstall")
        if installation._sha_file(member) != trusted_files[name]:
            raise installation.OfflineInstallError("installed bytes changed during uninstall")
        member.unlink()
    receipt = root / installation._RECEIPT
    if not stat.S_ISREG(receipt.lstat().st_mode):
        raise installation.OfflineInstallError("installed receipt changed during uninstall")
    receipt.unlink()
    for name in sorted(directories, key=lambda value: (-value.count("/"), value)):
        directory = root.joinpath(*name.split("/"))
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise installation.OfflineInstallError("installed directory changed during uninstall")
        directory.rmdir()
    # Unknown files or directories added after the last inspection remain intact.
    root.rmdir()


def uninstall_verified_release(
    archive: str | Path,
    destination: str | Path,
    *,
    expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, object]:
    """Remove only an unchanged, independently pinned install; never touch extra data."""
    source_sha = installation._canonical_sha(expected_source_sha, field="source SHA", length=40)
    package_sha = installation._canonical_sha(expected_package_sha256, field="package SHA", length=64)
    root = _existing_product_root(Path(destination))
    archive_path = Path(archive)
    try:
        if not stat.S_ISREG(archive_path.lstat().st_mode):
            raise installation.OfflineInstallError("archive must be a regular file")
    except OSError as exc:
        raise installation.OfflineInstallError("original release ZIP unavailable") from exc
    if installation._sha_file(archive_path) != package_sha:
        raise installation.OfflineInstallError("original release ZIP digest differs")
    verified = release_package.verify_windows_package(
        archive_path, expected_source_sha=source_sha, require_source_manifest=True
    )
    if (
        verified.get("status") != "PASS" or verified.get("source_sha") != source_sha
        or verified.get("package_sha256") != package_sha
    ):
        raise installation.OfflineInstallError("original release ZIP not independently verified")
    # Never trust the unsigned installed receipt alone to authorize deletion.
    # Reconstruct the exact canonical ZIP inventory from independently pinned bytes.
    trusted_files: dict[str, str] = {}
    with zipfile.ZipFile(archive_path) as package:
        infos = package.infolist()
        if not 2 <= len(infos) <= installation._MAX_FILES:
            raise installation.OfflineInstallError("invalid source release inventory")
        total = 0
        for member in infos:
            if member.is_dir() or not member.filename.startswith(installation._PREFIX):
                raise installation.OfflineInstallError("unexpected release member")
            name = member.filename[len(installation._PREFIX):]
            if not name or name in trusted_files or member.file_size < 0:
                raise installation.OfflineInstallError("duplicate or unsupported release member")
            total += member.file_size
            if total > installation._MAX_TOTAL_BYTES:
                raise installation.OfflineInstallError("release exceeds installed byte limit")
            digest = hashlib.sha256()
            copied = 0
            with package.open(member) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    copied += len(chunk)
                    if copied > member.file_size:
                        raise installation.OfflineInstallError("release member exceeded declared size")
                    digest.update(chunk)
            if copied != member.file_size:
                raise installation.OfflineInstallError("truncated release member")
            trusted_files[name] = digest.hexdigest()
    count = _installed_members(
        root, expected_source_sha=source_sha, package_sha256=package_sha,
        trusted_files=trusted_files,
    )
    if installation._sha_file(archive_path) != package_sha:
        raise installation.OfflineInstallError("original release ZIP changed during verification")

    quarantine = Path(tempfile.mkdtemp(prefix=".autosport-uninstall-staging-", dir=root.parent))
    staged = quarantine / "payload"
    moved = False
    removed = False
    deletion_started = False
    try:
        installation._publish_directory_no_replace(root, staged)
        moved = True
        # Recheck after the atomic move: never delete newly injected user files.
        if _installed_members(staged, expected_source_sha=source_sha, package_sha256=package_sha, trusted_files=trusted_files) != count:
            raise installation.OfflineInstallError("installed inventory changed before uninstall")
        deletion_started = True
        _remove_pinned_inventory(staged, trusted_files)
        removed = True
        return {
            "status": "UNINSTALLED", "files_removed": count,
            "source_sha": source_sha, "package_sha256": package_sha,
            "real_money_execution": False,
        }
    finally:
        if moved and not deletion_started and staged.exists() and not root.exists():
            # Never replace a competing destination. Keep quarantine if restore is unsafe.
            try:
                installation._publish_directory_no_replace(staged, root)
            except (OSError, installation.OfflineInstallError):
                pass
        if not staged.exists():
            try:
                quarantine.rmdir()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pinned PAPER-only Autosport uninstall")
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--package-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        report = uninstall_verified_release(
            args.archive, args.destination,
            expected_source_sha=args.source_sha, expected_package_sha256=args.package_sha256,
        )
    except (installation.OfflineInstallError, OSError, ValueError, TypeError, RuntimeError, zipfile.BadZipFile):
        print("uninstall=FAIL_CLOSED reason=unverified_or_modified_install")
        return 3
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
