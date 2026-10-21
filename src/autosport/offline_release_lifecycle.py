"""Read back and safely uninstall only the exact, independently pinned PAPER release.

This never changes owner workspaces, bookmaker accounts or economic state.
Unknown files or mutated release bytes fail closed instead of being deleted.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import zipfile
import zlib
from pathlib import Path

from . import release_package
from .offline_release_install import (
    OfflineInstallError, _RECEIPT, _publish_directory_no_replace, _safe_root,
    _is_redirected_install_path,
)

_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_ENTRIES = 4096


def _regular_file(path: Path) -> bool:
    try:
        return (not _is_redirected_install_path(path)
                and stat.S_ISREG(path.lstat().st_mode))
    except OSError:
        return False


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspection_root(destination: str | Path) -> Path:
    path = Path(destination)
    # The clean-install admission rejects both pre-existing destinations and
    # symlink ancestors. Reuse that check for an already-installed directory.
    if not path.is_dir() or path.is_symlink():
        raise OfflineInstallError("release installation directory is unavailable")
    try:
        _safe_root(path / ".autosport-inspection-sentinel")
    except OfflineInstallError as exc:
        raise OfflineInstallError("release installation path is unsafe") from exc
    return path


def _trusted_archive_inventory(
    archive: str | Path, *, expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, str]:
    """Verify the ORIGINAL ZIP and derive the only authorized installed files."""
    original = Path(archive)
    if not _regular_file(original) or _hash_file(original) != expected_package_sha256:
        raise OfflineInstallError("original release archive is unavailable or differs")
    try:
        verified = release_package.verify_windows_package(
            original, expected_source_sha=expected_source_sha,
            require_source_manifest=True,
        )
        if (
            verified.get("status") != "PASS"
            or verified.get("source_sha") != expected_source_sha
            or verified.get("package_sha256") != expected_package_sha256
        ):
            raise OfflineInstallError("original release verification did not match anchors")
        trusted: dict[str, str] = {}
        windows_keys: set[str] = set()
        total = 0
        with zipfile.ZipFile(original) as package:
            infos = package.infolist()
            if not 2 <= len(infos) <= _MAX_ENTRIES:
                raise OfflineInstallError("invalid original release inventory count")
            for member in infos:
                if member.is_dir() or not member.filename.startswith("Autosport-V1/"):
                    raise OfflineInstallError("invalid original release member")
                name, windows_key = release_package._validate_windows_member(member.filename)
                if (
                    not name or name == _RECEIPT or name in trusted
                    or windows_key in windows_keys or member.file_size < 0
                    or total + member.file_size > 2 * 1024 * 1024 * 1024
                ):
                    raise OfflineInstallError("invalid original release member identity")
                total += member.file_size
                windows_keys.add(windows_key)
                digest = hashlib.sha256()
                size = 0
                with package.open(member) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(block)
                        if size > member.file_size:
                            raise OfflineInstallError("release entry exceeded declared size")
                        digest.update(block)
                if size != member.file_size:
                    raise OfflineInstallError("incomplete original release member")
                trusted[name] = digest.hexdigest()
        if not {"Autosport.exe", "BUILD_INFO.json"}.issubset(trusted):
            raise OfflineInstallError("original archive lacks product identity")
        if _hash_file(original) != expected_package_sha256:
            raise OfflineInstallError("original release changed during inspection")
        return trusted
    except (OSError, ValueError, EOFError, zipfile.BadZipFile, zlib.error) as exc:
        raise OfflineInstallError("original archive could not be verified") from exc


def _assert_release_parents_unredirected(root: Path, member: Path) -> None:
    """Refuse nested symlinks and Windows junctions before accessing release bytes."""
    ancestor = member.parent
    while ancestor != root:
        if _is_redirected_install_path(ancestor):
            raise OfflineInstallError("release member parent is redirected")
        try:
            mode = ancestor.lstat().st_mode
        except OSError as exc:
            raise OfflineInstallError("release member parent is unavailable") from exc
        if not stat.S_ISDIR(mode):
            raise OfflineInstallError("release member parent is not a real directory")
        ancestor = ancestor.parent


def inspect_installed_release(
    destination: str | Path, *, archive: str | Path,
    expected_source_sha: str, expected_package_sha256: str,
) -> dict[str, object]:
    """Verify installed files against independently pinned original release ZIP."""
    root = _inspection_root(destination)
    if not re.fullmatch(r"[0-9a-f]{40}", expected_source_sha or ""):
        raise OfflineInstallError("invalid independent source anchor")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_package_sha256 or ""):
        raise OfflineInstallError("invalid independent package anchor")
    receipt_path = root / _RECEIPT
    if (_is_redirected_install_path(receipt_path) or not _regular_file(receipt_path)
            or receipt_path.stat().st_size > _MAX_RECEIPT_BYTES):
        raise OfflineInstallError("missing or invalid installed-release receipt")
    try:
        receipt = release_package._decode_json_object(
            receipt_path.read_bytes(), "installed-release receipt"
        )
    except (ValueError, OSError) as exc:
        raise OfflineInstallError("invalid installed-release receipt") from exc
    if (
        set(receipt) != {
            "schema_version", "product", "source_sha", "package_sha256",
            "files", "real_money_execution", "human_tested", "nvda_verified",
        }
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["product"] != "Autosport"
        or receipt["source_sha"] != expected_source_sha
        or receipt["package_sha256"] != expected_package_sha256
        or any(receipt[key] is not False for key in (
            "real_money_execution", "human_tested", "nvda_verified"
        ))
    ):
        raise OfflineInstallError("installed-release receipt differs from trusted anchors")
    files = receipt["files"]
    trusted_files = _trusted_archive_inventory(
        archive, expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    if files != trusted_files:
        raise OfflineInstallError("installed receipt inventory differs from original release")
    if type(files) is not dict or not 2 <= len(files) <= _MAX_ENTRIES:
        raise OfflineInstallError("invalid installed-release file inventory")
    if not {"Autosport.exe", "BUILD_INFO.json"}.issubset(files):
        raise OfflineInstallError("installed-release inventory lacks product identity")

    permitted_dirs = {""}
    windows_keys: set[str] = set()
    for name, digest in files.items():
        if type(name) is not str or type(digest) is not str or not re.fullmatch(
            r"[a-f0-9]{64}", digest
        ):
            raise OfflineInstallError("invalid installed-release inventory member")
        try:
            relative, windows_key = release_package._validate_windows_member(
                "Autosport-V1/" + name
            )
        except ValueError as exc:
            raise OfflineInstallError("unsafe installed-release inventory path") from exc
        if relative != name or windows_key in windows_keys:
            raise OfflineInstallError("ambiguous Windows installed-release path")
        windows_keys.add(windows_key)
        parts = name.split("/")
        for index in range(1, len(parts)):
            permitted_dirs.add("/".join(parts[:index]))
        member = root.joinpath(*parts)
        _assert_release_parents_unredirected(root, member)
        if (_is_redirected_install_path(member) or not _regular_file(member)
                or _hash_file(member) != digest):
            raise OfflineInstallError("installed release has missing or modified bytes")
    actual_names = {_RECEIPT}
    count = 0
    for parent, subdirs, filenames in os.walk(root, topdown=True, followlinks=False):
        relative_dir = Path(parent).relative_to(root)
        prefix = "" if relative_dir == Path(".") else relative_dir.as_posix()
        if prefix not in permitted_dirs or _is_redirected_install_path(Path(parent)):
            raise OfflineInstallError("unowned or redirected installed-release directory")
        for dirname in subdirs:
            count += 1
            candidate = Path(parent) / dirname
            key = "/".join(part for part in (prefix, dirname) if part)
            if (count > 2 * _MAX_ENTRIES or _is_redirected_install_path(candidate)
                    or not stat.S_ISDIR(candidate.lstat().st_mode)
                    or key not in permitted_dirs):
                raise OfflineInstallError("unexpected installed-release directory")
        for filename in filenames:
            count += 1
            if count > 2 * _MAX_ENTRIES:
                raise OfflineInstallError("installed-release inventory exceeds safe limit")
            candidate = Path(parent) / filename
            relative = "/".join(part for part in (prefix, filename) if part)
            if _is_redirected_install_path(candidate) or not _regular_file(candidate):
                raise OfflineInstallError("unsafe installed-release filesystem entry")
            actual_names.add(relative)
    if actual_names != set(files) | {_RECEIPT}:
        raise OfflineInstallError("unowned files in installed release")
    return {
        "status": "VERIFIED", "file_count": len(files),
        "source_sha": expected_source_sha,
        "package_sha256": expected_package_sha256,
        "real_money_execution": False,
        "human_tested": False, "nvda_verified": False,
    }


def uninstall_verified_release(
    destination: str | Path, *, archive: str | Path,
    expected_source_sha: str, expected_package_sha256: str,
) -> dict[str, object]:
    """Move exact product aside, independently re-verify, then delete only listed files.

    On a failed second verification, restore the intact original directory;
    no unfamiliar/user data is ever intentionally deleted.
    """
    root = _inspection_root(destination)
    result = inspect_installed_release(
        root, archive=archive, expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    identity = root.stat()
    staging = Path(tempfile.mkdtemp(
        prefix=".autosport-uninstall-", dir=root.parent
    ))
    moved = staging / "verified-product"
    deletion_started = False
    try:
        if root.stat().st_ino != identity.st_ino or root.stat().st_dev != identity.st_dev:
            raise OfflineInstallError("release directory changed before removal")
        # A competing same-user worker may create the quarantine entry after
        # mkdtemp.  POSIX rename() would replace an empty directory there.
        # Preserve the same atomic no-clobber invariant as install/upgrade.
        _publish_directory_no_replace(root, moved)
        if moved.stat().st_ino != identity.st_ino or moved.stat().st_dev != identity.st_dev:
            raise OfflineInstallError("release directory identity changed")
        inspect_installed_release(
            moved, archive=archive, expected_source_sha=expected_source_sha,
            expected_package_sha256=expected_package_sha256,
        )
        receipt = release_package._decode_json_object(
            (moved / _RECEIPT).read_bytes(), "installed-release receipt"
        )
        # Only the hash-verified product inventory may be removed. Never
        # recursively delete an entire directory with untrusted extra members.
        for name in sorted(receipt["files"]):
            member = moved / Path(*name.split("/"))
            # A file may still look regular after a parent directory was
            # swapped for a symlink. Refuse traversal outside the quarantine.
            _assert_release_parents_unredirected(moved, member)
            if (_is_redirected_install_path(member) or not _regular_file(member)
                    or _hash_file(member) != receipt["files"][name]):
                raise OfflineInstallError("installed file changed during removal")
            deletion_started = True
            member.unlink()
        (moved / _RECEIPT).unlink()
        dirs = {
            "/".join(name.split("/")[:i])
            for name in receipt["files"]
            for i in range(1, len(name.split("/")))
        }
        for name in sorted(dirs, key=lambda value: (-value.count("/"), value)):
            directory = moved / Path(*name.split("/"))
            if _is_redirected_install_path(directory):
                raise OfflineInstallError("installed directory redirected during removal")
            directory.rmdir()
        moved.rmdir()
        return {**result, "status": "UNINSTALLED"}
    except Exception:
        # If failure happened before any deletion, the original tree is intact.
        # Never overwrite a concurrently recreated destination.
        if not deletion_started and moved.is_dir() and not root.exists():
            try:
                if (moved / _RECEIPT).is_file():
                    inspect_installed_release(
                        moved, archive=archive, expected_source_sha=expected_source_sha,
                        expected_package_sha256=expected_package_sha256,
                    )
                    _publish_directory_no_replace(moved, root)
            except (OfflineInstallError, OSError, ValueError):
                pass
        raise
    finally:
        try:
            staging.rmdir()
        except OSError:
            # A nonempty quarantine remains recoverable on partial I/O error.
            pass
