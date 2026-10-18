"""Pinned installed-state trust boundary for *preflight-only* Windows updates.

The expected manifest SHA-256 MUST come from a trusted, non-updatable
installed executable/configuration authority, never from the candidate ZIP,
user-writable update manifest, or caller-provided network metadata. This module
cannot grant installation permission; it neither mutates state nor signs data.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .windows_update_admission import InstalledWindowsProduct
from .windows_update_snapshot import (
    WindowsUpdateSnapshot, WindowsUpdateSnapshotError, _native_change_time_100ns,
    _owned_fd_stream,
)

_MAX_MANIFEST_BYTES = 8192
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_HEX_KEY = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_FIELDS = frozenset({
    "format", "product", "version", "schema", "package_sha256",
    "trusted_ed25519_public_key_hex",
})
_FORMAT = "autosport-installed-update-authority-v1"


class InstalledWindowsTrustError(ValueError):
    """Installed authority is missing, altered, noncanonical or unverifiable."""


@dataclass(frozen=True, slots=True)
class PinnedInstalledWindowsAuthority:
    installed: InstalledWindowsProduct
    trusted_public_key: bytes
    manifest_sha256: str
    _source_path: Path = field(repr=False)
    _file_identity: tuple[int, int] = field(repr=False)
    _file_metadata: tuple[int, int, int] = field(repr=False)
    _change_time_100ns: int | None = field(default=None, repr=False)
    install_authorized: bool = False

    def __post_init__(self) -> None:
        if self.install_authorized is not False or type(self.install_authorized) is not bool:
            raise InstalledWindowsTrustError("installed trust preflight cannot authorize installation")
        if type(self.installed) is not InstalledWindowsProduct:
            raise InstalledWindowsTrustError("installed identity is not canonical")
        self.installed.__post_init__()
        if type(self.trusted_public_key) is not bytes or len(self.trusted_public_key) != 32:
            raise InstalledWindowsTrustError("invalid installed public key")
        if type(self.manifest_sha256) is not str or not _SHA256.fullmatch(self.manifest_sha256):
            raise InstalledWindowsTrustError("invalid manifest digest")
        if type(self._source_path) is not type(Path()) or not self._source_path.is_absolute():
            raise InstalledWindowsTrustError("installed manifest root is not anchored")
        if (type(self._file_identity) is not tuple or len(self._file_identity) != 2
                or any(type(part) is not int or part < 0 for part in self._file_identity)
                or self._file_identity[1] == 0):
            raise InstalledWindowsTrustError("installed manifest identity is invalid")
        if (type(self._file_metadata) is not tuple or len(self._file_metadata) != 3
                or any(type(part) is not int or part < 0 for part in self._file_metadata)
                or self._file_metadata[0] == 0 or self._file_metadata[0] > _MAX_MANIFEST_BYTES):
            raise InstalledWindowsTrustError("installed manifest metadata is invalid")
        if ((self._change_time_100ns is not None and
             (type(self._change_time_100ns) is not int or self._change_time_100ns <= 0))
            or (os.name == "nt" and self._change_time_100ns is None)):
            raise InstalledWindowsTrustError("installed manifest change-time is invalid")


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InstalledWindowsTrustError("duplicate installed manifest field")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise InstalledWindowsTrustError("noncanonical installed manifest constant")


def _identity(info: os.stat_result) -> tuple[int, int]:
    if type(info.st_dev) is not int or type(info.st_ino) is not int or info.st_dev < 0 or info.st_ino <= 0:
        raise InstalledWindowsTrustError("installed manifest filesystem identity unavailable")
    return info.st_dev, info.st_ino


def load_pinned_installed_update_authority(
    manifest_path: str | Path, *, expected_manifest_sha256: str
) -> PinnedInstalledWindowsAuthority:
    """Read exact installed authority only under an out-of-band trusted digest.

    Refuses symlinks, path replacement, duplicate JSON keys, noncanonical
    bytes, partial writes, unsupported schema and unpinned key substitution.
    Callers must not obtain ``expected_manifest_sha256`` from this same file.
    """
    if type(expected_manifest_sha256) is not str or not _SHA256.fullmatch(expected_manifest_sha256):
        raise InstalledWindowsTrustError("trusted manifest digest must be canonical")
    if type(manifest_path) not in (str, type(Path())):
        raise InstalledWindowsTrustError("installed manifest path must be canonical")
    # Anchor once: a concurrent cwd change must never retarget installed trust.
    try:
        path = Path(os.path.abspath(os.fspath(manifest_path)))
    except (OSError, UnicodeError, ValueError, TypeError):
        raise InstalledWindowsTrustError("installed manifest path is invalid") from None
    try:
        entry = path.lstat()
        if not stat.S_ISREG(entry.st_mode) or not 0 < entry.st_size <= _MAX_MANIFEST_BYTES:
            raise InstalledWindowsTrustError("installed manifest must be a bounded regular file")
        entry_id = _identity(entry)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with _owned_fd_stream(fd) as stream:
            opened = os.fstat(stream.fileno())
            opened_change_time = _native_change_time_100ns(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _identity(opened) != entry_id:
                raise InstalledWindowsTrustError("installed manifest changed while opening")
            data = stream.read(_MAX_MANIFEST_BYTES + 1)
            completed = os.fstat(stream.fileno())
            completed_change_time = _native_change_time_100ns(stream.fileno())
            final_entry = path.lstat()
            if (
                _identity(completed) != entry_id
                or completed_change_time != opened_change_time
                or _identity(final_entry) != entry_id
                or not stat.S_ISREG(final_entry.st_mode)
                or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                != (completed.st_size, completed.st_mtime_ns, completed.st_ctime_ns)
                or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                != (final_entry.st_size, final_entry.st_mtime_ns, final_entry.st_ctime_ns)
            ):
                raise InstalledWindowsTrustError("installed manifest changed during read")
    except (OSError, UnicodeError, WindowsUpdateSnapshotError):
        raise InstalledWindowsTrustError("installed manifest could not be read") from None
    if not 0 < len(data) <= _MAX_MANIFEST_BYTES or len(data) != entry.st_size:
        raise InstalledWindowsTrustError("installed manifest length changed")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected_manifest_sha256):
        raise InstalledWindowsTrustError("installed manifest differs from trusted digest")
    try:
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_reject_constant)
        if type(parsed) is not dict or parsed.keys() != _FIELDS:
            raise InstalledWindowsTrustError("installed manifest has invalid fields")
        if parsed["format"] != _FORMAT or type(parsed["format"]) is not str or parsed["product"] != "Autosport" or type(parsed["product"]) is not str:
            raise InstalledWindowsTrustError("installed manifest product or format mismatch")
        key_hex = parsed["trusted_ed25519_public_key_hex"]
        if type(key_hex) is not str or not _HEX_KEY.fullmatch(key_hex):
            raise InstalledWindowsTrustError("installed public key is noncanonical")
        installed = InstalledWindowsProduct(
            version=parsed["version"], schema=parsed["schema"],
            package_sha256=parsed["package_sha256"],
        )
        if data != (json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode("ascii"):
            raise InstalledWindowsTrustError("installed manifest JSON is noncanonical")
        return PinnedInstalledWindowsAuthority(
            installed=installed,
            trusted_public_key=bytes.fromhex(key_hex),
            manifest_sha256=expected_manifest_sha256,
            _source_path=path,
            _file_identity=entry_id,
            _file_metadata=(opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns),
            _change_time_100ns=opened_change_time,
        )
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise InstalledWindowsTrustError("installed manifest is invalid") from None


def assess_update_with_installed_trust(
    manifest_path: str | Path, *, expected_manifest_sha256: str,
    snapshot: object, candidate_version: str, candidate_schema: int,
    expected_source_sha: str, signature: bytes,
    supported_schema_migrations: tuple[tuple[int, int], ...],
):
    """Bind update signature verification to an independently pinned installed key.

    Returns only a signed *preflight* result; it cannot perform installation.
    """
    from .windows_update_signature import assess_signed_windows_update

    if type(snapshot) is not WindowsUpdateSnapshot:
        raise InstalledWindowsTrustError("a canonical pinned snapshot is required")
    # The candidate and the installed trust root form one joint admission
    # boundary. Both must be valid before any expensive signature work.
    snapshot.recheck()
    authority = load_pinned_installed_update_authority(
        manifest_path, expected_manifest_sha256=expected_manifest_sha256
    )
    result = assess_signed_windows_update(
        authority.installed, snapshot, candidate_version=candidate_version,
        candidate_schema=candidate_schema, expected_source_sha=expected_source_sha,
        signature=signature, trusted_public_key=authority.trusted_public_key,
        supported_schema_migrations=supported_schema_migrations,
    )
    # The trusted manifest must remain the same filesystem-backed authority
    # throughout signature verification. This is still NOT an installer commit
    # gate: the real updater must revalidate inside its own transaction.
    after = load_pinned_installed_update_authority(
        authority._source_path, expected_manifest_sha256=expected_manifest_sha256
    )
    if after != authority:
        raise InstalledWindowsTrustError("installed authority changed during preflight")
    # The second installed-manifest read itself takes time; a concurrent actor
    # could mutate the pinned candidate after the signature verifier's last
    # check. Never return a success-shaped result with either root stale.
    snapshot.recheck()
    return result
