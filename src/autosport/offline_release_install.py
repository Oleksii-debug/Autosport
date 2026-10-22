"""Install a canonical, independently SHA-pinned PAPER release to a NEW path only.

No existing install, real-money state, or user workspace can be overwritten.
Physical Windows/NVDA and production signing are separate acceptance gates.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

from . import release_package

_RECEIPT = ".autosport-installed-release.json"
_PREFIX = "Autosport-V1/"
_MAX_FILES = 4096
_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


class OfflineInstallError(ValueError):
    """Release is not safe to install without changing the incumbent tree."""


def _canonical_sha(value: str, *, field: str, length: int) -> str:
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{%d}" % length, value) is None:
        raise OfflineInstallError(f"invalid {field}")
    return value


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for piece in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(piece)
    return digest.hexdigest()


def _is_redirected_install_path(path: Path) -> bool:
    """Reject symlinks AND Windows junction/reparse paths without following them.

    Path.is_symlink() alone does not identify Windows directory junctions.
    This preflight checks existing components; atomic directory publication
    remains the independent no-replace protection against target races.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OfflineInstallError("install path cannot be safely inspected") from exc
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_root(destination: Path) -> Path:
    if not destination.is_absolute():
        raise OfflineInstallError("install destination must be absolute")
    if destination.parent == destination or not destination.name:
        raise OfflineInstallError("install destination is not a product directory")
    parent = destination.parent
    if not parent.is_dir() or _is_redirected_install_path(parent) or _is_redirected_install_path(destination):
        raise OfflineInstallError("install parent/destination is unsafe")
    if destination.exists():
        raise OfflineInstallError("existing installation must not be overwritten")
    # A normal-looking immediate parent can still be reached through a
    # symlink higher in the absolute path. Refuse such paths before staging
    # any verified owner release outside the requested directory hierarchy.
    ancestor = parent
    while True:
        if _is_redirected_install_path(ancestor):
            raise OfflineInstallError("install path traverses a symlink")
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent
    return destination


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish without replacing a directory created by another actor.

    The caller's earlier destination.exists() check is advisory only. POSIX
    os.rename can replace an empty destination directory, unlike Windows.
    Fail closed when the platform lacks an atomic no-replace primitive.
    """
    if os.name == "nt":
        # Windows os.rename never replaces an existing destination. Translate
        # its collision to the same fail-closed domain error used on Linux,
        # without leaking an owner-controlled local path through WinError.
        try:
            os.rename(source, destination)
        except FileExistsError as exc:
            raise OfflineInstallError(
                "installation destination appeared during publication"
            ) from exc
        return
    if sys.platform != "linux":
        raise OfflineInstallError("atomic no-replace installation is unsupported")
    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise OfflineInstallError("atomic no-replace installation is unavailable")
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    # AT_FDCWD=-100, RENAME_NOREPLACE=1; no fallback to replacing rename.
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        if error in (errno.EEXIST, errno.ENOTEMPTY):
            raise OfflineInstallError("installation destination appeared during publication")
        raise OSError(error, os.strerror(error))


def install_verified_release(
    archive: str | Path,
    destination: str | Path,
    *,
    expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, object]:
    """Verify exact release bytes against independent anchors; publish once.

    This deliberately does not perform upgrades, migrations or rollbacks.
    """
    expected_source_sha = _canonical_sha(expected_source_sha, field="source SHA", length=40)
    expected_package_sha256 = _canonical_sha(expected_package_sha256, field="package SHA", length=64)
    archive = Path(archive)
    destination = _safe_root(Path(destination))
    try:
        if not stat.S_ISREG(archive.lstat().st_mode):
            raise OfflineInstallError("archive must be a regular non-symlink file")
    except OSError as exc:
        raise OfflineInstallError("archive is unavailable") from exc
    if _sha_file(archive) != expected_package_sha256:
        raise OfflineInstallError("archive differs from independently pinned SHA-256")
    verified = release_package.verify_windows_package(
        archive, expected_source_sha=expected_source_sha, require_source_manifest=True
    )
    if (
        verified.get("status") != "PASS"
        or verified.get("package_sha256") != expected_package_sha256
        or verified.get("source_sha") != expected_source_sha
    ):
        raise OfflineInstallError("canonical release verification did not match trust anchors")

    temp_root: Path | None = None
    try:
        temp_root = Path(tempfile.mkdtemp(prefix=".autosport-install-staging-", dir=destination.parent))
        # Verification authenticates a snapshot, not a mutable pathname.
        # Preserve exactly those SHA-pinned wire bytes inside private staging;
        # extract exclusively from that copy, never by reopening the original.
        archive_snapshot = temp_root / "verified-archive.zip"
        snapshot_digest = hashlib.sha256()
        snapshot_size = 0
        with archive.open("rb") as reader, archive_snapshot.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                snapshot_size += len(chunk)
                if snapshot_size > _MAX_TOTAL_BYTES:
                    raise OfflineInstallError("release archive exceeds bounded snapshot size")
                snapshot_digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if snapshot_digest.hexdigest() != expected_package_sha256:
            raise OfflineInstallError("archive changed before trusted extraction snapshot")
        payload_root = temp_root / "payload"
        payload_root.mkdir(mode=0o700)
        file_hashes: dict[str, str] = {}
        windows_keys: set[str] = {_RECEIPT.casefold()}
        total = 0
        with zipfile.ZipFile(archive_snapshot) as source:
            infos = source.infolist()
            if len(infos) > _MAX_FILES or not infos:
                raise OfflineInstallError("unsupported release member count")
            for info in infos:
                name = info.filename
                if (
                    not name.startswith(_PREFIX) or name.endswith("/")
                    or info.is_dir() or "\\" in name
                ):
                    raise OfflineInstallError("unsafe release entry")
                relative = name[len(_PREFIX):]
                parts = relative.split("/")
                if not parts or any(p in {"", ".", ".."} for p in parts):
                    raise OfflineInstallError("unsafe release path")
                if relative == _RECEIPT or relative in file_hashes:
                    raise OfflineInstallError("reserved or duplicated release path")
                # Independently enforce the Windows namespace during extraction,
                # even if an upstream package verifier is accidentally bypassed.
                try:
                    canonical, windows_key = release_package._validate_windows_member(name)
                except ValueError as exc:
                    raise OfflineInstallError("unsafe Windows release path") from exc
                if canonical != relative or windows_key in windows_keys:
                    raise OfflineInstallError("ambiguous Windows release path")
                windows_keys.add(windows_key)
                if info.file_size < 0 or total + info.file_size > _MAX_TOTAL_BYTES:
                    raise OfflineInstallError("release exceeds bounded installed size")
                total += info.file_size
                target = payload_root.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with source.open(info) as reader, target.open("xb") as output:
                    copied = 0
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        copied += len(chunk)
                        if copied > info.file_size:
                            raise OfflineInstallError("release entry expanded beyond declared size")
                        digest.update(chunk)
                        output.write(chunk)
                    if copied != info.file_size:
                        raise OfflineInstallError("truncated release entry")
                    output.flush()
                    os.fsync(output.fileno())
                file_hashes[relative] = digest.hexdigest()
        if "Autosport.exe" not in file_hashes or "BUILD_INFO.json" not in file_hashes:
            raise OfflineInstallError("release lacks product executable/identity")
        receipt = {
            "schema_version": 1,
            "product": "Autosport",
            "source_sha": expected_source_sha,
            "package_sha256": expected_package_sha256,
            "files": {name: file_hashes[name] for name in sorted(file_hashes)},
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        with (payload_root / _RECEIPT).open("x", encoding="utf-8") as receipt_file:
            json.dump(receipt, receipt_file, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            receipt_file.write("\n")
            receipt_file.flush()
            os.fsync(receipt_file.fileno())
        if _sha_file(archive) != expected_package_sha256:
            raise OfflineInstallError("archive changed during staged installation")
        if destination.exists() or destination.is_symlink():
            raise OfflineInstallError("installation appeared during staging")
        _publish_directory_no_replace(payload_root, destination)
        return {
            "status": "INSTALLED", "source_sha": expected_source_sha,
            "package_sha256": expected_package_sha256,
            "file_count": len(file_hashes), "real_money_execution": False,
        }
    except (OSError, zipfile.BadZipFile, EOFError, zlib.error) as exc:
        raise OfflineInstallError("installation failed without replacing an existing release") from exc
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pinned offline Autosport clean install (PAPER-only)")
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--package-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        receipt = install_verified_release(
            args.archive, args.destination, expected_source_sha=args.source_sha,
            expected_package_sha256=args.package_sha256,
        )
    except (OfflineInstallError, OSError, zipfile.BadZipFile, EOFError, zlib.error, RuntimeError, TypeError, ValueError):
        print("install=FAIL_CLOSED reason=unsafe_or_unverified_release")
        return 3
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
