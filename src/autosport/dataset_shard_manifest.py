from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .dataset_snapshot_lineage import (
    DatasetSnapshotLineageAuthority as _DatasetSnapshotLineageAuthority,
    membership_manifest_sha256 as _membership_manifest_sha256,
)

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
_LINEAGE_RECORD = _DatasetSnapshotLineageAuthority.record
_LINEAGE_RECORD_CODE = _LINEAGE_RECORD.__code__
_MEMBERSHIP_MANIFEST_SHA256_CODE = _membership_manifest_sha256.__code__


class DatasetShardManifestError(RuntimeError):
    """Dataset shard descriptor or byte-verification contract failed."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if (
        len(text) != 64
        or text != text.lower()
        or any(char not in _HEX for char in text)
    ):
        raise ValueError(
            f"{name} must be a canonical lowercase SHA-256 hex string"
        )
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
        if (
            raw.get("kind") != _SHARD_KIND
            or type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1
        ):
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
            type(item) is not DatasetShardDescriptor for item in self.shards
        ):
            raise ValueError(
                "shards must be a tuple of exact DatasetShardDescriptor values"
            )
        detached = tuple(
            DatasetShardDescriptor(
                ordinal=item.ordinal,
                shard_id=item.shard_id,
                relative_path=item.relative_path,
                byte_size=item.byte_size,
                content_sha256=item.content_sha256,
                event_start_utc=item.event_start_utc,
                event_end_utc=item.event_end_utc,
            )
            for item in self.shards
        )
        ordered = tuple(sorted(detached, key=lambda item: item.ordinal))
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
        if (
            raw.get("kind") != _DESCRIPTOR_SET_KIND
            or type(raw.get("schema_version")) is not int
            or raw["schema_version"] != 1
        ):
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

        return _membership_manifest_sha256(self.member_sha256)


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


def _resolve_shard_root(shard_root: str | Path) -> Path:
    lexical_root = Path(shard_root).expanduser()
    if lexical_root.is_symlink():
        raise DatasetShardManifestError("shard_root itself must not be a symlink")
    try:
        root = lexical_root.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise DatasetShardManifestError("shard_root must be an existing directory") from exc
    if not root.is_dir():
        raise DatasetShardManifestError("shard_root must be an existing directory")
    return root


_SHARD_HELPER_WITNESSES = (
    ("shard root", "_resolve_shard_root", _resolve_shard_root, _resolve_shard_root.__code__),
    ("manifest canonicalization", "canonical_shard_manifest", canonical_shard_manifest, canonical_shard_manifest.__code__),
    ("descriptor verification", "_verify_descriptor_bytes", _verify_descriptor_bytes, _verify_descriptor_bytes.__code__),
    ("candidate path", "_candidate_path", _candidate_path, _candidate_path.__code__),
    ("stable-file observer", "_observe_stable_regular_file", _observe_stable_regular_file, _observe_stable_regular_file.__code__),
    ("relative path", "_relative_path", _relative_path, _relative_path.__code__),
    ("stat identity", "_stat_identity", _stat_identity, _stat_identity.__code__),
)


def _require_shard_helper_authority(
    _witnesses: tuple[tuple[str, str, Any, Any], ...] = _SHARD_HELPER_WITNESSES,
) -> None:
    """Fail closed before positive shard verification can dispatch through replaced helpers."""

    namespace = globals()
    for label, global_name, expected, expected_code in _witnesses:
        if (
            namespace.get(global_name) is not expected
            or getattr(expected, "__code__", None) is not expected_code
        ):
            raise DatasetShardManifestError(
                f"canonical dataset shard {label} executable changed"
            )


def verify_shard_files(
    shard_root: str | Path,
    shards: Iterable[DatasetShardDescriptor],
) -> DatasetShardManifest:
    """Recompute every shard byte-size/hash before returning canonical membership.

    The returned DTO is not an authority token. Positive DatasetSnapshot truth still
    comes only from composition with DatasetSnapshotLineageAuthority.
    """

    _require_shard_helper_authority()
    root = _resolve_shard_root(shard_root)
    _require_shard_helper_authority()
    manifest = canonical_shard_manifest(shards)
    _require_shard_helper_authority()
    for descriptor in manifest.shards:
        _verify_descriptor_bytes(root, descriptor)
        _require_shard_helper_authority()
    return manifest


def _require_registered_manifest(
    authority: "DatasetSnapshotLineageAuthority",
    *,
    snapshot_id: str,
    manifest: DatasetShardManifest,
    _authority_type: type = _DatasetSnapshotLineageAuthority,
    _record_reader: Any = _LINEAGE_RECORD,
    _record_reader_code: Any = _LINEAGE_RECORD_CODE,
    _manifest_digest: Any = _membership_manifest_sha256,
    _manifest_digest_code: Any = _MEMBERSHIP_MANIFEST_SHA256_CODE,
    _canonical_text: Any = _text,
    _canonical_text_code: Any = _text.__code__,
    _canonical_utc_instant: Any = _utc_instant,
    _canonical_utc_instant_code: Any = _utc_instant.__code__,
) -> "DatasetSnapshotLineageRecord":
    """Read canonical lineage through import-time captured, non-virtual authority."""

    if type(authority) is not _authority_type:
        raise DatasetShardManifestError(
            "canonical DatasetSnapshotLineageAuthority is required"
        )

    def require_external_callable_identity() -> None:
        if (
            _authority_type.record is not _record_reader
            or getattr(_record_reader, "__code__", None) is not _record_reader_code
        ):
            raise DatasetShardManifestError(
                "canonical DatasetSnapshotLineageAuthority record executable changed"
            )
        if (
            _membership_manifest_sha256 is not _manifest_digest
            or getattr(_manifest_digest, "__code__", None) is not _manifest_digest_code
        ):
            raise DatasetShardManifestError(
                "canonical DatasetSnapshotLineage manifest executable changed"
            )
        if (
            _text is not _canonical_text
            or getattr(_canonical_text, "__code__", None) is not _canonical_text_code
        ):
            raise DatasetShardManifestError(
                "canonical dataset shard text executable changed"
            )
        if (
            _utc_instant is not _canonical_utc_instant
            or getattr(_canonical_utc_instant, "__code__", None)
            is not _canonical_utc_instant_code
        ):
            raise DatasetShardManifestError(
                "canonical dataset shard time executable changed"
            )

    require_external_callable_identity()
    record = _record_reader(
        authority,
        _canonical_text(snapshot_id, "snapshot_id"),
    )
    require_external_callable_identity()
    if record is None:
        raise DatasetShardManifestError("DatasetSnapshot has no canonical lineage proof")
    if tuple(record.member_sha256) != manifest.member_sha256:
        raise DatasetShardManifestError(
            "verified shard descriptor commitments do not match DatasetSnapshot membership"
        )
    require_external_callable_identity()
    observed_manifest_sha256 = _manifest_digest(manifest.member_sha256)
    require_external_callable_identity()
    if record.manifest_sha256 != observed_manifest_sha256:
        raise DatasetShardManifestError(
            "verified shard manifest does not match DatasetSnapshot manifest identity"
        )
    try:
        require_external_callable_identity()
        causal_cutoff = _canonical_utc_instant(record.causal_cutoff, "causal_cutoff")
        require_external_callable_identity()
    except ValueError as exc:
        raise DatasetShardManifestError(
            "canonical DatasetSnapshot causal cutoff is invalid"
        ) from exc
    for descriptor in manifest.shards:
        require_external_callable_identity()
        if (
            _canonical_utc_instant(descriptor.event_end_utc, "event_end_utc")
            > causal_cutoff
        ):
            raise DatasetShardManifestError(
                "dataset shard event range exceeds DatasetSnapshot causal cutoff "
                f"for {descriptor.shard_id}"
            )
        require_external_callable_identity()
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
    root = _resolve_shard_root(shard_root)
    payload = _verify_descriptor_bytes(root, descriptor, capture_bytes=True)
    assert payload is not None
    return payload


def _install_registered_shard_entrypoints():
    require_registered = _require_registered_manifest
    verify_files = verify_shard_files
    resolve_root = _resolve_shard_root
    verify_descriptor = _verify_descriptor_bytes
    canonical_text = _text
    shard_helper_guard = _require_shard_helper_authority
    require_registered_code = require_registered.__code__
    verify_files_code = verify_files.__code__
    resolve_root_code = resolve_root.__code__
    verify_descriptor_code = verify_descriptor.__code__
    canonical_text_code = canonical_text.__code__
    shard_helper_guard_code = shard_helper_guard.__code__

    def require_canonical_helpers() -> None:
        witnesses = (
            ("registered manifest", require_registered, require_registered_code),
            ("shard verification", verify_files, verify_files_code),
            ("shard root", resolve_root, resolve_root_code),
            ("descriptor verification", verify_descriptor, verify_descriptor_code),
            ("canonical text", canonical_text, canonical_text_code),
            ("helper authority", shard_helper_guard, shard_helper_guard_code),
        )
        for label, helper, expected_code in witnesses:
            if getattr(helper, "__code__", None) is not expected_code:
                raise DatasetShardManifestError(
                    f"canonical dataset shard {label} executable changed"
                )
        shard_helper_guard()

    def sealed_verify_registered_dataset_shards(
        authority: "DatasetSnapshotLineageAuthority",
        *,
        snapshot_id: str,
        shard_root: str | Path,
        shards: Iterable[DatasetShardDescriptor],
    ) -> "DatasetSnapshotLineageRecord":
        require_canonical_helpers()
        manifest = verify_files(shard_root, shards)
        require_canonical_helpers()
        record = require_registered(
            authority,
            snapshot_id=snapshot_id,
            manifest=manifest,
        )
        require_canonical_helpers()
        return record

    def sealed_read_registered_shard_bytes(
        authority: "DatasetSnapshotLineageAuthority",
        *,
        snapshot_id: str,
        shard_root: str | Path,
        shards: Iterable[DatasetShardDescriptor],
        shard_id: str,
    ) -> bytes:
        require_canonical_helpers()
        descriptors = tuple(shards)
        manifest = verify_files(shard_root, descriptors)
        require_canonical_helpers()
        require_registered(
            authority,
            snapshot_id=snapshot_id,
            manifest=manifest,
        )
        require_canonical_helpers()
        wanted = canonical_text(shard_id, "shard_id")
        descriptor = next(
            (item for item in manifest.shards if item.shard_id == wanted),
            None,
        )
        if descriptor is None:
            raise DatasetShardManifestError(f"unknown shard_id: {wanted}")
        root = resolve_root(shard_root)
        require_canonical_helpers()
        payload = verify_descriptor(root, descriptor, capture_bytes=True)
        require_canonical_helpers()
        assert payload is not None
        return payload

    return sealed_verify_registered_dataset_shards, sealed_read_registered_shard_bytes


verify_registered_dataset_shards, read_registered_shard_bytes = (
    _install_registered_shard_entrypoints()
)
del _install_registered_shard_entrypoints