"""Fail-closed, owner-invoked offline workspace backup and new-root restore.

A backup contains potentially private product data. Keep archives on trusted local
storage; this interface never transmits them and grants no bookmaker capability.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .workspace_lock import WorkspaceEconomicLock

SCHEMA = 1
MAX_FILES = 4096
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MANIFEST = "AUTOSPORT_BACKUP_MANIFEST.json"
_SKIP = frozenset({WorkspaceEconomicLock.FILE_NAME})
_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
    | {f"{prefix}{digit}" for prefix in ("com", "lpt") for digit in "¹²³"}
)


class WorkspaceBackupError(ValueError):
    """Invalid source, archive or target; never indicate restore success."""


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _regular(path: Path, *, label: str) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WorkspaceBackupError(f"{label} must be an unaliased regular file")
    return info


def _safe_relative(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise WorkspaceBackupError("invalid backup member path")
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise WorkspaceBackupError("nonportable backup member name") from exc
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in name.split("/")) or path.as_posix() != name:
        raise WorkspaceBackupError("noncanonical backup member path")
    if any(
        part.endswith((".", " "))
        or part.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES
        or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        for part in path.parts
    ):
        raise WorkspaceBackupError("nonportable backup member name")
    if name.casefold() == _MANIFEST.casefold() or any(part.casefold() in {reserved.casefold() for reserved in _SKIP} for part in path.parts):
        raise WorkspaceBackupError("reserved backup member name")
    return path


def _write_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, _TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)


def _read_regular_stable(path: Path) -> bytes:
    before = _regular(path, label="workspace entry")
    if before.st_size > MAX_FILE_BYTES:
        raise WorkspaceBackupError("workspace file exceeds backup limit")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.name != "nt" and not nofollow:
        raise WorkspaceBackupError("no-follow filesystem open unavailable")
    with os.fdopen(os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_BINARY", 0)), "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise WorkspaceBackupError("workspace entry identity changed")
        data = handle.read(MAX_FILE_BYTES + 1)
        after = os.fstat(handle.fileno())
    final = _regular(path, label="workspace entry")
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if len(data) > MAX_FILE_BYTES or len(data) != before.st_size or identity(before) != identity(after) or identity(after) != identity(final):
        raise WorkspaceBackupError("workspace entry changed during backup")
    return data


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def backup_workspace(workspace: str | Path, destination: str | Path) -> dict[str, object]:
    """Create a restrictive, hash-inventoried ZIP outside the source workspace.

    The canonical economic writer lock is taken throughout snapshot collection.
    Other non-cooperating processes must be stopped by the owner first.
    """
    root, target = Path(workspace), Path(destination)
    if not root.is_dir() or root.is_symlink():
        raise WorkspaceBackupError("workspace must be an existing real directory")
    if target.exists() or target.is_symlink():
        raise WorkspaceBackupError("backup destination already exists")
    if target.resolve(strict=False).is_relative_to(root.resolve(strict=True)):
        raise WorkspaceBackupError("backup destination must be outside the workspace")
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise WorkspaceBackupError("backup destination parent must be an existing directory")
    temporary: Path | None = None
    try:
        with WorkspaceEconomicLock(root):
            files: list[tuple[str, bytes]] = []
            seen_windows_names: set[str] = set()
            total = 0
            for folder, directory_names, file_names in os.walk(root, followlinks=False):
                folder_path = Path(folder)
                for directory in sorted(directory_names):
                    item = folder_path / directory
                    if not stat.S_ISDIR(item.lstat().st_mode):
                        raise WorkspaceBackupError("workspace directory alias or special file")
                    relative_directory = item.relative_to(root).as_posix()
                    _safe_relative(relative_directory)
                    folded_directory = relative_directory.casefold()
                    if folded_directory in seen_windows_names:
                        raise WorkspaceBackupError("Windows-case-colliding workspace paths")
                    seen_windows_names.add(folded_directory)
                directory_names.sort()
                for filename in sorted(file_names):
                    source = folder_path / filename
                    name = source.relative_to(root).as_posix()
                    if name in _SKIP:
                        continue
                    _safe_relative(name)
                    folded_name = name.casefold()
                    if folded_name in seen_windows_names:
                        raise WorkspaceBackupError("Windows-case-colliding workspace paths")
                    seen_windows_names.add(folded_name)
                    data = _read_regular_stable(source)
                    total += len(data)
                    if len(files) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                        raise WorkspaceBackupError("workspace backup quota exceeded")
                    files.append((name, data))
            records = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files]
            manifest = {"kind": "autosport.workspace.backup", "schema": SCHEMA, "files": records, "total_bytes": total}
            if len(_canonical_bytes(manifest)) > 512 * 1024:
                raise WorkspaceBackupError("backup manifest exceeds verification limit")
            fd, temp_name = tempfile.mkstemp(prefix=".autosport-backup-", suffix=".tmp", dir=target.parent)
            temporary = Path(temp_name)
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "w+b") as stream:
                with zipfile.ZipFile(stream, "w") as archive:
                    for name, data in files:
                        _write_member(archive, name, data)
                    _write_member(archive, _MANIFEST, _canonical_bytes(manifest))
                stream.flush()
                os.fsync(stream.fileno())
        os.link(temporary, target)
        return {"file_count": len(files), "total_bytes": total, "archive_sha256": _sha256_path(target), "private_data": True}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validated_members(source: Path) -> tuple[list[tuple[str, bytes]], int]:
    _regular(source, label="backup archive")
    with zipfile.ZipFile(source, "r") as archive:
        entries = archive.infolist()
        if not entries or len(entries) > MAX_FILES + 1:
            raise WorkspaceBackupError("invalid backup archive member count")
        names = [item.filename for item in entries]
        if len(set(names)) != len(names) or names.count(_MANIFEST) != 1:
            raise WorkspaceBackupError("duplicate or missing backup manifest")
        if len({name.casefold() for name in names}) != len(names):
            raise WorkspaceBackupError("Windows-case-colliding backup members")
        if any(item.is_dir() or item.file_size > MAX_FILE_BYTES or item.flag_bits & 0x1 for item in entries):
            raise WorkspaceBackupError("unsafe backup archive member")
        for name in names:
            if name != _MANIFEST:
                _safe_relative(name)
        raw_manifest = archive.read(_MANIFEST)
        if len(raw_manifest) > 512 * 1024:
            raise WorkspaceBackupError("oversized backup manifest")
        try:
            manifest = json.loads(raw_manifest.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid JSON constant")))
        except (ValueError, UnicodeError) as exc:
            raise WorkspaceBackupError("invalid backup manifest JSON") from exc
        if not isinstance(manifest, dict) or set(manifest) != {"kind", "schema", "files", "total_bytes"} or manifest.get("kind") != "autosport.workspace.backup" or type(manifest.get("schema")) is not int or manifest["schema"] != SCHEMA or type(manifest.get("total_bytes")) is not int:
            raise WorkspaceBackupError("invalid backup manifest schema")
        records = manifest["files"]
        if not isinstance(records, list) or len(records) != len(entries) - 1:
            raise WorkspaceBackupError("incomplete backup manifest")
        if len(raw_manifest) != len(_canonical_bytes(manifest)) or raw_manifest != _canonical_bytes(manifest):
            raise WorkspaceBackupError("noncanonical backup manifest")
        files: list[tuple[str, bytes]] = []
        total = 0
        for record in records:
            if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
                raise WorkspaceBackupError("invalid backup file record")
            name = record["path"]
            _safe_relative(name)
            size, digest = record["size"], record["sha256"]
            if type(size) is not int or size < 0 or size > MAX_FILE_BYTES or not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise WorkspaceBackupError("invalid backup file identity")
            if name not in names:
                raise WorkspaceBackupError("missing archived file")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise WorkspaceBackupError("backup exceeds total quota")
            data = archive.read(name)
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise WorkspaceBackupError("backup integrity mismatch")
            files.append((name, data))
        if len({name for name, _ in files}) != len(files) or set(name for name, _ in files) != set(names) - {_MANIFEST} or total != manifest["total_bytes"]:
            raise WorkspaceBackupError("backup inventory mismatch")
        return files, total


def _publish_new_workspace(stage: Path, root: Path) -> None:
    """Atomically install a verified tree without replacing a concurrent directory.

    Plain POSIX os.rename may replace an existing EMPTY directory even when
    existence was checked immediately beforehand. Linux RENAME_NOREPLACE and
    Windows' no-overwrite directory rename close that collision window.
    Unsupported platforms fail closed rather than risking owner data.
    """
    if os.name == "nt":
        try:
            os.rename(stage, root)
        except FileExistsError as exc:
            raise WorkspaceBackupError("restore target already exists; refuse overwrite") from exc
        return
    if sys.platform.startswith("linux"):
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except (AttributeError, OSError) as exc:
            raise WorkspaceBackupError("atomic no-replace restore unavailable") from exc
        renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        renameat2.restype = ctypes.c_int
        # AT_FDCWD=-100; RENAME_NOREPLACE=1. Neither path is a user command.
        result = renameat2(-100, os.fsencode(stage), -100, os.fsencode(root), 1)
        if result == 0:
            return
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise WorkspaceBackupError("restore target already exists; refuse overwrite")
        if code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise WorkspaceBackupError("atomic no-replace restore unavailable")
        raise OSError(code, os.strerror(code))
    raise WorkspaceBackupError("atomic no-replace restore unavailable")


def restore_workspace(source: str | Path, destination: str | Path) -> dict[str, object]:
    """Verify the entire archive before creating a fresh destination workspace.

    Existing workspaces are never overwritten; no economic runtime is activated.
    """
    archive, root = Path(source), Path(destination)
    if root.exists() or root.is_symlink():
        raise WorkspaceBackupError("restore target already exists; refuse overwrite")
    if not root.parent.is_dir() or root.parent.is_symlink():
        raise WorkspaceBackupError("restore parent must be an existing directory")
    files, total = _validated_members(archive)
    with tempfile.TemporaryDirectory(prefix=".autosport-restoring-", dir=root.parent) as staged:
        stage = Path(staged)
        for name, data in files:
            path = stage.joinpath(*PurePosixPath(name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(path, 0o600)
        if root.exists() or root.is_symlink():
            raise WorkspaceBackupError("restore destination appeared during validation")
        _publish_new_workspace(stage, root)
    return {"file_count": len(files), "total_bytes": total, "private_data": True, "real_money_execution": False}
