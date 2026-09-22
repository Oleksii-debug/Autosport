from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable, Mapping

if TYPE_CHECKING:
    from .dataset_snapshot_lineage import (
        DatasetSnapshotLineageAuthority,
        DatasetSnapshotLineageRecord,
    )


_HEX = frozenset("0123456789abcdef")
_SHARD_KIND = "autosport-dataset-shard-member-v1"
_DESCRIPTOR_SET_KIND = "autosport-dataset-shard-descriptor-set-v1"
_READ_CHUNK_BYTES = 1024 * 1024
_WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_WINDOWS_FORBIDDEN_FILENAME_CHARS = frozenset('<>"|?*')


class DatasetShardManifestError(RuntimeError):
    """Dataset shard descriptor or byte-verification contract failed."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ValueError(f"{name} must be a canonical SHA-256 hex string")
    return text


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _utc_instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_utc(value: object, name: str) -> str:
    text = _text(value, name)
    if not text.endswith("Z"):
        raise ValueError(f"{name} must be explicit UTC with a Z suffix")
    parsed = _utc_instant(text, name)
    if parsed.microsecond:
        return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _relative_path(value: object) -> str:
    text = _text(value, "relative_path")
    if "\\" in text or text.startswith("/") or text.endswith("/") or "//" in text:
        raise ValueError("relative_path must be one portable canonical POSIX relative path")
    raw_parts = text.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("relative_path must not contain empty, dot, or parent segments")
    if any(":" in part for part in raw_parts):
        raise ValueError("relative_path must not contain Windows drive/stream separators")
    for part in raw_parts:
        if (
            part.endswith((".", " "))
            or any(ord(char) < 32 for char in part)
            or any(char in _WINDOWS_FORBIDDEN_FILENAME_CHARS for char in part)
        ):
            raise ValueError("relative_path contains a non-portable Windows path segment")
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_STEMS:
            raise ValueError("relative_path contains a reserved Windows device name")
    path = PurePosixPath(text)
    if path.is_absolute() or tuple(path.parts) != tuple(raw_parts):
        raise ValueError("relative_path must be one portable canonical POSIX relative path")
    return text


def _byte_size(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("byte_size must be a non-negative integer")
    return value


def _ordinal(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("ordinal must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class DatasetShardDescriptor:
    """Canonical descriptor for exactly one immutable dataset shard."""

    ordinal: int
    shard_id: str
    relative_path: str
    byte_size: int
    content_sha256: str
    event_start_utc: str
    event_end_utc: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ordinal", _ordinal(self.ordinal))
        object.__setattr__(self, "shard_id", _text(self.shard_id, "shard_id"))
        object.__setattr__(self, "relative_path", _relative_path(self.relative_path))
        object.__setattr__(self, "byte_size", _byte_size(self.byte_size))
        object.__setattr__(
            self,
            "content_sha256",
            _sha256(self.content_sha256, "content_sha256"),
        )
        start = _canonical_utc(self.event_start_utc, "event_start_utc")
        end = _canonical_utc(self.event_end_utc, "event_end_utc")
        if datetime.fromisoformat(end[:-1] + "+00:00") < datetime.fromisoformat(
            start[:-1] + "+00:00"
        ):
            raise ValueError("event_end_utc must not precede event_start_utc")
        object.__setattr__(self, "event_start_utc", start)
        object.__setattr__(self, "event_end_utc", end)

    def payload(self) -> dict[str, Any]:
        return {
            "kind": _SHARD_KIND,
            "schema_version": 1,
            "ordinal": self.ordinal,
            "shard_id": self.shard_id,
            "relative_path": self.relative_path,
            "byte_size": self.byte_size,
            "content_sha256": self.content_sha256,
            "event_start_utc": self.event_start_utc,
            "event_end_utc": self.event_end_utc,
        }

    @classmethod
    def from_payload(cls, raw: object) -> "DatasetShardDescriptor":
        if type(raw) is not dict:
            raise ValueError("dataset shard descriptor must be an object")
        required = {
            "kind",
            "schema_version",
            "ordinal",
            "shard_id",
            "relative_path",
            "byte_size",
            "content_sha256",
            "event_start_utc",
            "event_end_utc",
        }
        if set(raw) != required:
            raise ValueError("dataset shard descriptor fields mismatch")
        if raw.get("kind") != _SHARD_KIND or raw.get("schema_version") != 1:
            raise ValueError("dataset shard descriptor schema mismatch")
        return cls(
            ordinal=raw["ordinal"],
            shard_id=raw["shard_id"],
            relative_path=raw["relative_path"],
            byte_size=raw["byte_size"],
            content_sha256=raw["content_sha256"],
            event_start_utc=raw["event_start_utc"],
            event_end_utc=raw["event_end_utc"],
        )

    @property
    def member_sha256(self) -> str:
        """Commit byte identity and every causal/path descriptor field as one member."""

        return _digest(self.payload())


@dataclass(frozen=True, slots=True)
class DatasetShardManifest:
    """Canonical input-order-independent shard set for DatasetSnapshot membership."""

    shards: tuple[DatasetShardDescriptor, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.shards, tuple) or any(
            not isinstance(item, DatasetShardDescriptor) for item in self.shards
        ):
            raise ValueError("shards must be a tuple of DatasetShardDescriptor values")
        ordered = tuple(sorted(self.shards, key=lambda item: item.ordinal))
        ordinals = tuple(item.ordinal for item in ordered)
        if ordinals != tuple(range(len(ordered))):
            raise ValueError("shard ordinals must be contiguous from zero")
        ids = tuple(item.shard_id for item in ordered)
        paths = tuple(item.relative_path for item in ordered)
        portable_paths = tuple(path.casefold() for path in paths)
        if len(ids) != len(set(ids)):
            raise ValueError("shard_id values must be unique")
        if len(paths) != len(set(paths)) or len(portable_paths) != len(set(portable_paths)):
            raise ValueError("relative_path values must be unique across portable filesystems")
        object.__setattr__(self, "shards", ordered)

    @property
    def member_sha256(self) -> tuple[str, ...]:
        return tuple(item.member_sha256 for item in self.shards)

    def payload(self) -> dict[str, Any]:
        return {
            "kind": _DESCRIPTOR_SET_KIND,
            "schema_version": 1,
            "shards": [item.payload() for item in self.shards],
        }

    @classmethod
    def from_payload(cls, raw: object) -> "DatasetShardManifest":
        if type(raw) is not dict or set(raw) != {"kind", "schema_version", "shards"}:
            raise ValueError("dataset shard manifest fields mismatch")
        if raw.get("kind") != _DESCRIPTOR_SET_KIND or raw.get("schema_version") != 1:
            raise ValueError("dataset shard manifest schema mismatch")
        raw_shards = raw.get("shards")
        if type(raw_shards) is not list:
            raise ValueError("dataset shard manifest shards must be a list")
        return cls(tuple(DatasetShardDescriptor.from_payload(item) for item in raw_shards))

    @property
    def descriptor_set_sha256(self) -> str:
        return _digest(self.payload())

    def membership_manifest_sha256(self) -> str:
        """Delegate the outer manifest identity to the canonical lineage authority."""

        from .dataset_snapshot_lineage import membership_manifest_sha256

        return membership_manifest_sha256(self.member_sha256)


def canonical_shard_manifest(
    shards: Iterable[DatasetShardDescriptor],
) -> DatasetShardManifest:
    """Canonicalize caller ordering without weakening exact descriptor identity."""

    return DatasetShardManifest(tuple(shards))


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _candidate_path(root: Path, relative_path: str) -> Path:
    current = root
    for part in _relative_path(relative_path).split("/"):
        current = current / part
        if current.is_symlink():
            raise DatasetShardManifestError(
                f"dataset shard path traverses a symlink: {relative_path}"
            )
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise DatasetShardManifestError(
            f"dataset shard path is missing or escapes shard_root: {relative_path}"
        ) from exc
    return resolved


def _observe_stable_regular_file(
    path: Path,
    *,
    capture_bytes: bool,
) -> tuple[int, str, bytes | None]:
    """Read one stable regular file once, streaming its digest.

    Ordinary verification retains no shard payload in memory. The bytes are captured
    only for the explicit read API, whose contract is to return those exact bytes.
    """

    before_path = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before_path.st_mode):
        raise DatasetShardManifestError(f"dataset shard is not a regular file: {path}")
    digest = hashlib.sha256()
    byte_size = 0
    chunks: list[bytes] | None = [] if capture_bytes else None
    try:
        with path.open("rb") as handle:
            before_fd = os.fstat(handle.fileno())
            if _stat_identity(before_fd) != _stat_identity(before_path):
                raise DatasetShardManifestError(
                    f"dataset shard changed between path verification and open: {path}"
                )
            while True:
                chunk = handle.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                byte_size += len(chunk)
                if chunks is not None:
                    chunks.append(chunk)
            after_fd = os.fstat(handle.fileno())
    except OSError as exc:
        raise DatasetShardManifestError(f"failed reading dataset shard: {path}") from exc
    after_path = path.stat(follow_symlinks=False)
    identity = _stat_identity(before_path)
    if _stat_identity(after_fd) != identity or _stat_identity(after_path) != identity:
        raise DatasetShardManifestError(f"dataset shard changed while being verified: {path}")
    payload = b"".join(chunks) if chunks is not None else None
    return byte_size, digest.hexdigest(), payload


def _verify_descriptor_bytes(
    root: Path,
    descriptor: DatasetShardDescriptor,
    *,
    capture_bytes: bool = False,
) -> bytes | None:
    path = _candidate_path(root, descriptor.relative_path)
    byte_size, observed_sha256, payload = _observe_stable_regular_file(
        path,
        capture_bytes=capture_bytes,
    )
    if byte_size != descriptor.byte_size:
        raise DatasetShardManifestError(
            f"dataset shard byte-size mismatch for {descriptor.shard_id}"
        )
    if observed_sha256 != descriptor.content_sha256:
        raise DatasetShardManifestError(
            f"dataset shard SHA-256 mismatch for {descriptor.shard_id}"
        )
    return payload


def verify_shard_files(
    shard_root: str | Path,
    shards: Iterable[DatasetShardDescriptor],
) -> DatasetShardManifest:
    """Recompute every shard byte-size/hash before returning canonical membership.

    The returned DTO is not an authority token. Positive DatasetSnapshot truth still
    comes only from composition with DatasetSnapshotLineageAuthority.
    """

    lexical_root = Path(shard_root).expanduser()
    if lexical_root.is_symlink():
        raise DatasetShardManifestError("shard_root itself must not be a symlink")
    try:
        root = lexical_root.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise DatasetShardManifestError("shard_root must be an existing directory") from exc
    if not root.is_dir():
        raise DatasetShardManifestError("shard_root must be an existing directory")
    manifest = canonical_shard_manifest(shards)
    for descriptor in manifest.shards:
        _verify_descriptor_bytes(root, descriptor)
    return manifest


def _require_registered_manifest(
    authority: "DatasetSnapshotLineageAuthority",
    *,
    snapshot_id: str,
    manifest: DatasetShardManifest,
) -> "DatasetSnapshotLineageRecord":
    from .dataset_snapshot_lineage import DatasetSnapshotLineageAuthority

    if type(authority) is not DatasetSnapshotLineageAuthority:
        raise DatasetShardManifestError(
            "canonical DatasetSnapshotLineageAuthority is required"
        )
    record = DatasetSnapshotLineageAuthority.record(
        authority,
        _text(snapshot_id, "snapshot_id"),
    )
    if record is None:
        raise DatasetShardManifestError("DatasetSnapshot has no canonical lineage proof")
    if tuple(record.member_sha256) != manifest.member_sha256:
        raise DatasetShardManifestError(
            "verified shard descriptor commitments do not match DatasetSnapshot membership"
        )
    if record.manifest_sha256 != manifest.membership_manifest_sha256():
        raise DatasetShardManifestError(
            "verified shard manifest does not match DatasetSnapshot manifest identity"
        )
    try:
        causal_cutoff = _utc_instant(record.causal_cutoff, "causal_cutoff")
    except ValueError as exc:
        raise DatasetShardManifestError(
            "canonical DatasetSnapshot causal cutoff is invalid"
        ) from exc
    for descriptor in manifest.shards:
        if _utc_instant(descriptor.event_end_utc, "event_end_utc") > causal_cutoff:
            raise DatasetShardManifestError(
                "dataset shard event range exceeds DatasetSnapshot causal cutoff "
                f"for {descriptor.shard_id}"
            )
    return record


def verify_registered_dataset_shards(
    authority: "DatasetSnapshotLineageAuthority",
    *,
    snapshot_id: str,
    shard_root: str | Path,
    shards: Iterable[DatasetShardDescriptor],
) -> "DatasetSnapshotLineageRecord":
    """Bind current shard bytes to one already-canonical DatasetSnapshot record.

    This function deliberately re-verifies bytes instead of trusting a caller-built
    DatasetShardManifest. It adds no durable authority: the exact DatasetSnapshot
    lineage record remains the source of restart-safe membership truth.
    """

    manifest = verify_shard_files(shard_root, shards)
    return _require_registered_manifest(
        authority,
        snapshot_id=snapshot_id,
        manifest=manifest,
    )


def read_registered_shard_bytes(
    authority: "DatasetSnapshotLineageAuthority",
    *,
    snapshot_id: str,
    shard_root: str | Path,
    shards: Iterable[DatasetShardDescriptor],
    shard_id: str,
) -> bytes:
    """Return bytes only after current files and canonical lineage agree exactly."""

    descriptors = tuple(shards)
    manifest = verify_shard_files(shard_root, descriptors)
    _require_registered_manifest(
        authority,
        snapshot_id=snapshot_id,
        manifest=manifest,
    )
    wanted = _text(shard_id, "shard_id")
    descriptor = next((item for item in manifest.shards if item.shard_id == wanted), None)
    if descriptor is None:
        raise DatasetShardManifestError(f"unknown shard_id: {wanted}")
    root = Path(shard_root).expanduser().resolve(strict=True)
    payload = _verify_descriptor_bytes(root, descriptor, capture_bytes=True)
    assert payload is not None
    return payload
