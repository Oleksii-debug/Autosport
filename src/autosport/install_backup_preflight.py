"""Plan-6 read-only backup/transition preflight; NOT a backup or updater engine.

The two file inventories must come from the already-authoritative Plan-5
snapshot/backup manifest reader. This component NEVER creates archives, copies
state, deletes files, or authorizes installation on its own. The installer must
hold the canonical workspace lock from inventory capture through commit.

An inventory mapping alone is NOT cryptographic proof of who created the backup:
its provenance, separate credential escrow and signature/update trust are gates
of the existing canonical owners, not capabilities invented here.
"""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping


class InstallPreflightError(ValueError):
    """Reject the transition before the first destructive installation step."""


@dataclass(frozen=True, slots=True)
class BackupPreflightReadback:
    previous_source_sha: str
    previous_version: str
    previous_schema: int
    file_count: int
    total_bytes: int
    # Fingerprint of the canonical file inventory; NEVER a release/update approval.
    inventory_sha256: str


def _require_sha(value: object, length: int, label: str) -> str:
    if (not isinstance(value, str) or len(value) != length
            or any(c not in "0123456789abcdef" for c in value)):
        raise InstallPreflightError(f"invalid {label}")
    return value


def _require_schema(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise InstallPreflightError(f"invalid {label}")
    return value


def _require_version(value: object, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise InstallPreflightError(f"invalid {label}")
    # Retain exact old-version text in the readback. Version suffixes such as
    # -prehuman are allowed, but incomparable suffix changes do NOT authorize
    # a positive update by themselves.
    head, sep, suffix = value.partition("-")
    parts = head.split(".")
    if (len(parts) != 3 or any(not p.isascii() or not p.isdecimal()
                               or len(p) > 9 or (len(p) > 1 and p[0] == "0")
                               for p in parts)
            or (sep and (not suffix or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-."
                                          for c in suffix)))):
        raise InstallPreflightError(f"invalid {label}")
    return tuple(int(x) for x in parts)  # type: ignore[return-value]


def require_non_downgrade_transition(
    *,
    installed_version: str,
    target_version: str,
    installed_source_sha: str,
    target_source_sha: str,
    installed_schema: int,
    target_schema: int,
    approved_schema_edges: frozenset[tuple[int, int]],
) -> None:
    """Pure source contract: reject downgrade or unapproved state migration.

    A positive result is NOT update approval: signed package authentication,
    trusted previous-version identity and backup readback remain mandatory.
    """
    old = _require_version(installed_version, "installed version")
    new = _require_version(target_version, "target version")
    _require_sha(installed_source_sha, 40, "installed source identity")
    _require_sha(target_source_sha, 40, "target source identity")
    old_schema = _require_schema(installed_schema, "installed schema")
    new_schema = _require_schema(target_schema, "target schema")
    if not isinstance(approved_schema_edges, frozenset) or any(
        not isinstance(item, tuple) or len(item) != 2 or
        any(type(x) is not int or x < 1 for x in item)
        for item in approved_schema_edges
    ):
        raise InstallPreflightError("invalid canonical migration approvals")
    if new < old or new_schema < old_schema:
        raise InstallPreflightError("version or state schema downgrade is forbidden")
    if new == old and (target_version != installed_version or target_source_sha != installed_source_sha):
        raise InstallPreflightError("same-version identity change needs an explicit release authority")
    if new_schema != old_schema and (old_schema, new_schema) not in approved_schema_edges:
        raise InstallPreflightError("state schema migration has no canonical approval")


def _safe_member(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise InstallPreflightError("invalid inventory member")
    if ("\\" in value or "\x00" in value or ":" in value or
            value.startswith("/") or "//" in value or
            any(ord(ch) < 32 for ch in value)):
        raise InstallPreflightError("unsafe inventory member")
    p = PurePosixPath(value)
    if (not p.parts or any(part in ("", ".", "..") or part.endswith((" ", "."))
                           for part in value.split("/")) or
            "/".join(p.parts) != value):
        raise InstallPreflightError("noncanonical inventory member")
    return value


def _canonical_files(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise InstallPreflightError("missing canonical inventory")
    files: dict[str, str] = {}
    windows_seen: set[str] = set()
    for key, digest in value.items():
        member = _safe_member(key)
        folded = member.casefold()
        if folded in windows_seen:
            raise InstallPreflightError("Windows inventory name collision")
        windows_seen.add(folded)
        files[member] = _require_sha(digest, 64, "inventory hash")
    return files


def _require_safe_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstallPreflightError("snapshot directory is missing or unreadable") from None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & reparse:
        raise InstallPreflightError("snapshot directory is not a plain directory")


def _scan(root: Path) -> dict[str, Path]:
    """Reject foreign, temporary, symlink/reparse or unlisted inventory members."""
    _require_safe_dir(root)
    found: dict[str, Path] = {}
    pending: list[tuple[Path, str]] = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            raise InstallPreflightError("snapshot directory cannot be enumerated") from None
        for entry in entries:
            relative = _safe_member(prefix + entry.name)
            path = Path(entry.path)
            try:
                info = path.lstat()
            except OSError:
                raise InstallPreflightError("snapshot member is missing or unreadable") from None
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if getattr(info, "st_file_attributes", 0) & reparse:
                raise InstallPreflightError("snapshot contains a reparse point")
            if stat.S_ISDIR(info.st_mode):
                pending.append((path, relative + "/"))
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                found[relative] = path
            else:
                raise InstallPreflightError("snapshot contains an unsafe filesystem object")
    return found


def _digest_regular_file(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise InstallPreflightError("snapshot file cannot be opened safely") from None
    try:
        initial = os.fstat(fd)
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
            raise InstallPreflightError("snapshot file has unsafe identity")
        h = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
        final = os.fstat(fd)
        path_stat = path.lstat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_mode)
        if identity(initial) != identity(final) or identity(final) != identity(path_stat):
            raise InstallPreflightError("snapshot file changed during verification")
        return h.hexdigest(), final.st_size
    except OSError:
        raise InstallPreflightError("snapshot file read interrupted") from None
    finally:
        os.close(fd)


def verify_backup_preflight(
    workspace: str | Path,
    backup_root: str | Path,
    *,
    live_manifest_files: Mapping[str, str],
    backup_manifest_files: Mapping[str, str],
    previous_source_sha: str,
    previous_version: str,
    previous_schema: int,
    trusted_inventory_sha256: str,
) -> BackupPreflightReadback:
    """Compare two already-manifested Plan-5 snapshots without copying state.

    Call only while the *existing* canonical workspace lock is held. This is
    neither a source of trusted backup manifests nor a restore capability.
    """
    old_sha = _require_sha(previous_source_sha, 40, "previous source identity")
    _require_version(previous_version, "previous version")
    schema = _require_schema(previous_schema, "previous schema")
    left = _canonical_files(live_manifest_files)
    right = _canonical_files(backup_manifest_files)
    if left != right:
        raise InstallPreflightError("backup manifest is incomplete or inconsistent")
    inventory = "".join(f"{key}\0{left[key]}\n" for key in sorted(left)).encode("utf-8")
    fingerprint = hashlib.sha256(inventory).hexdigest()
    if fingerprint != _require_sha(trusted_inventory_sha256, 64, "trusted inventory fingerprint"):
        raise InstallPreflightError("backup inventory identity differs from trusted authority")
    workspace = Path(workspace)
    backup_root = Path(backup_root)
    if not workspace.is_absolute() or not backup_root.is_absolute():
        raise InstallPreflightError("workspace and backup must be absolute paths")
    _require_safe_dir(workspace)
    _require_safe_dir(backup_root)
    for root in (workspace, backup_root):
        for ancestor in root.parents:
            _require_safe_dir(ancestor)
    resolved_workspace, resolved_backup = workspace.resolve(), backup_root.resolve()
    if (resolved_workspace == resolved_backup or
            resolved_workspace in resolved_backup.parents or
            resolved_backup in resolved_workspace.parents):
        raise InstallPreflightError("backup and workspace must be distinct independent roots")
    live_paths, copied_paths = _scan(workspace), _scan(backup_root)
    if set(live_paths) != set(left) or set(copied_paths) != set(right):
        raise InstallPreflightError("snapshot has missing or undeclared files")
    total_size = 0
    for member in sorted(left):
        live_digest, live_size = _digest_regular_file(live_paths[member])
        backup_digest, backup_size = _digest_regular_file(copied_paths[member])
        if live_digest != left[member] or backup_digest != left[member] or live_size != backup_size:
            raise InstallPreflightError("backup readback differs from canonical live state")
        total_size += live_size
    return BackupPreflightReadback(old_sha, previous_version, schema, len(left), total_size,
                                   fingerprint)


def verify_rollback_snapshot_preflight(
    workspace: str | Path,
    backup_root: str | Path,
    *,
    trusted_backup_manifest_files: Mapping[str, str],
    previous_source_sha: str,
    previous_version: str,
    previous_schema: int,
    trusted_inventory_sha256: str,
) -> BackupPreflightReadback:
    """Read-only validation of *old* state before canonical Plan-5 restore.

    Unlike the install/backup preflight, rollback MUST NOT compare the backup
    against current live bytes: interrupted migrations can leave live files
    missing, partially replaced or at a newer schema. ``trusted_*`` inputs
    MUST originate outside the archive from the existing immutable Plan-5
    manifest/transaction authority, under the existing workspace lock. They
    must never be synthesized from archive bytes during recovery.

    This check does not restore any files, choose a destination, create backup
    state, or grant release/update/restore authority. The canonical restore
    transaction still validates destination, journal, and post-restore readback.
    """
    old_sha = _require_sha(previous_source_sha, 40, "previous source identity")
    _require_version(previous_version, "previous version")
    schema = _require_schema(previous_schema, "previous schema")
    files = _canonical_files(trusted_backup_manifest_files)
    inventory = "".join(f"{key}\\0{files[key]}\\n" for key in sorted(files)).encode("utf-8")
    fingerprint = hashlib.sha256(inventory).hexdigest()
    if fingerprint != _require_sha(trusted_inventory_sha256, 64, "trusted inventory fingerprint"):
        raise InstallPreflightError("rollback inventory identity differs from trusted authority")

    workspace, backup_root = Path(workspace), Path(backup_root)
    if not workspace.is_absolute() or not backup_root.is_absolute():
        raise InstallPreflightError("workspace and backup must be absolute paths")

    # A failed update may have removed the workspace root, so check all
    # existing ancestors but do not require its leaf to exist. This also
    # rejects symlink/reparse redirection in every existing prefix.
    for candidate in (workspace, *workspace.parents):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise InstallPreflightError("workspace path cannot be verified safely") from None
        _require_safe_dir(candidate)

    _require_safe_dir(backup_root)
    for ancestor in backup_root.parents:
        _require_safe_dir(ancestor)
    resolved_workspace, resolved_backup = workspace.resolve(), backup_root.resolve()
    if (resolved_workspace == resolved_backup or
            resolved_workspace in resolved_backup.parents or
            resolved_backup in resolved_workspace.parents):
        raise InstallPreflightError("rollback backup and workspace must be independent roots")

    found = _scan(backup_root)
    if set(found) != set(files):
        raise InstallPreflightError("rollback backup has missing or undeclared files")
    total_size = 0
    for member in sorted(files):
        digest, size = _digest_regular_file(found[member])
        if digest != files[member]:
            raise InstallPreflightError("rollback backup differs from trusted old state")
        total_size += size
    return BackupPreflightReadback(old_sha, previous_version, schema, len(files), total_size,
                                   fingerprint)
