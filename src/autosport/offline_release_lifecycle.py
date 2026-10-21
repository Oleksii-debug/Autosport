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
from pathlib import Path

from . import release_package
from .offline_release_install import (
    OfflineInstallError, _RECEIPT, _publish_directory_no_replace, _safe_root,
)

_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_ENTRIES = 4096


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _independent_original_archive_inventory(
    archive: str | Path, *, expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, str]:
    """Prove deletion scope from independently SHA-pinned canonical ZIP bytes.

    An installed receipt can be changed by anyone with write access to the
    installation. Its recomputed hashes alone must never authorize deletion
    of additional user-owned paths.
    """
    source = Path(archive)
    if not _regular_file(source):
        raise OfflineInstallError("original release archive must be a regular file")
    if _hash_file(source) != expected_package_sha256:
        raise OfflineInstallError("original release archive SHA-256 mismatch")
    try:
        evidence = release_package.verify_windows_package(
            source, expected_source_sha=expected_source_sha, require_source_manifest=True,
        )
    except (OSError, ValueError, TypeError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OfflineInstallError("original canonical release verification failed") from exc
    if (
        type(evidence) is not dict or evidence.get("status") != "PASS"
        or evidence.get("source_sha") != expected_source_sha
        or evidence.get("package_sha256") != expected_package_sha256
    ):
        raise OfflineInstallError("original canonical release identity is unverified")
    inventory: dict[str, str] = {}
    total = 0
    try:
        with zipfile.ZipFile(source) as package:
            infos = package.infolist()
            if not 2 <= len(infos) <= _MAX_ENTRIES:
                raise OfflineInstallError("original release inventory is unsupported")
            for member in infos:
                full = member.filename
                if (
                    member.is_dir() or not full.startswith("Autosport-V1/")
                    or member.file_size < 0
                ):
                    raise OfflineInstallError("unexpected original release member")
                name = full[len("Autosport-V1/"):]
                if not name or name in inventory:
                    raise OfflineInstallError("duplicate original release member")
                total += member.file_size
                if total > 2 * 1024 * 1024 * 1024:
                    raise OfflineInstallError("original release exceeds size limit")
                digest = hashlib.sha256()
                copied = 0
                with package.open(member) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        copied += len(chunk)
                        if copied > member.file_size:
                            raise OfflineInstallError("original release member expanded")
                        digest.update(chunk)
                if copied != member.file_size:
                    raise OfflineInstallError("original release member truncated")
                inventory[name] = digest.hexdigest()
    except (OSError, EOFError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise OfflineInstallError("unreadable original release inventory") from exc
    if _hash_file(source) != expected_package_sha256:
        raise OfflineInstallError("original release archive changed during inspection")
    return inventory


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


def inspect_installed_release(
    destination: str | Path, *, expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, object]:
    """Verify receipt, every installed file, namespace and no unowned bytes."""
    root = _inspection_root(destination)
    if not re.fullmatch(r"[0-9a-f]{40}", expected_source_sha or ""):
        raise OfflineInstallError("invalid independent source anchor")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_package_sha256 or ""):
        raise OfflineInstallError("invalid independent package anchor")
    receipt_path = root / _RECEIPT
    if not _regular_file(receipt_path) or receipt_path.stat().st_size > _MAX_RECEIPT_BYTES:
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
        if not _regular_file(member) or _hash_file(member) != digest:
            raise OfflineInstallError("installed release has missing or modified bytes")
    actual_names = {_RECEIPT}
    count = 0
    for parent, subdirs, filenames in os.walk(root, topdown=True, followlinks=False):
        relative_dir = Path(parent).relative_to(root)
        prefix = "" if relative_dir == Path(".") else relative_dir.as_posix()
        if prefix not in permitted_dirs:
            raise OfflineInstallError("unowned installed-release directory")
        for dirname in subdirs:
            count += 1
            candidate = Path(parent) / dirname
            key = "/".join(part for part in (prefix, dirname) if part)
            if count > 2 * _MAX_ENTRIES or candidate.is_symlink() or key not in permitted_dirs:
                raise OfflineInstallError("unexpected installed-release directory")
        for filename in filenames:
            count += 1
            if count > 2 * _MAX_ENTRIES:
                raise OfflineInstallError("installed-release inventory exceeds safe limit")
            candidate = Path(parent) / filename
            relative = "/".join(part for part in (prefix, filename) if part)
            if not _regular_file(candidate):
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
    destination: str | Path, *, original_archive: str | Path,
    expected_source_sha: str, expected_package_sha256: str,
) -> dict[str, object]:
    """Move exact product aside, independently re-verify, then delete only listed files.

    On a failed second verification, restore the intact original directory;
    no unfamiliar/user data is ever intentionally deleted.
    """
    root = _inspection_root(destination)
    result = inspect_installed_release(
        root, expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    independently_proven_files = _independent_original_archive_inventory(
        original_archive, expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    receipt = release_package._decode_json_object(
        (root / _RECEIPT).read_bytes(), "installed-release receipt"
    )
    if receipt.get("files") != independently_proven_files:
        raise OfflineInstallError(
            "installed receipt is not bound to the original canonical release archive"
        )
    identity = root.stat()
    staging = Path(tempfile.mkdtemp(
        prefix=".autosport-uninstall-", dir=root.parent
    ))
    moved = staging / "verified-product"
    try:
        if root.stat().st_ino != identity.st_ino or root.stat().st_dev != identity.st_dev:
            raise OfflineInstallError("release directory changed before removal")
        os.rename(root, moved)
        if moved.stat().st_ino != identity.st_ino or moved.stat().st_dev != identity.st_dev:
            raise OfflineInstallError("release directory identity changed")
        inspect_installed_release(
            moved, expected_source_sha=expected_source_sha,
            expected_package_sha256=expected_package_sha256,
        )
        # The movable tree's receipt must still match the independently
        # verified archive inventory before a single named file is deleted.
        moved_receipt = release_package._decode_json_object(
            (moved / _RECEIPT).read_bytes(), "installed-release receipt"
        )
        if moved_receipt.get("files") != independently_proven_files:
            raise OfflineInstallError("installed release changed before removal")
        receipt = release_package._decode_json_object(
            (moved / _RECEIPT).read_bytes(), "installed-release receipt"
        )
        # Only the hash-verified product inventory may be removed. Never
        # recursively delete an entire directory with untrusted extra members.
        for name in sorted(receipt["files"]):
            (moved / Path(*name.split("/"))).unlink()
        (moved / _RECEIPT).unlink()
        dirs = {
            "/".join(name.split("/")[:i])
            for name in receipt["files"]
            for i in range(1, len(name.split("/")))
        }
        for name in sorted(dirs, key=lambda value: (-value.count("/"), value)):
            (moved / Path(*name.split("/"))).rmdir()
        moved.rmdir()
        return {**result, "status": "UNINSTALLED"}
    except Exception:
        # If failure happened before any deletion, the original tree is intact.
        # Never overwrite a concurrently recreated destination.
        if moved.is_dir() and not root.exists():
            try:
                if (moved / _RECEIPT).is_file():
                    inspect_installed_release(
                        moved, expected_source_sha=expected_source_sha,
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
