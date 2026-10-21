from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .release_sbom import validate_direct_dependency_metadata
from .release_dependency_graph import validate_build_host_dependency_graph

from .windows_update_release_metadata import (
    attach_update_identity,
    validate_requested_update_identity,
    validate_embedded_update_identity,
)

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_PACKAGE_PREFIX = "Autosport-V1/"
_PREHUMAN_BUILD_VERSION = "0.1.0-v1-prehuman"
_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
}
_WINDOWS_INVALID_CHARS = frozenset('<>:"\\|?*')
_WINDOWS_MAX_COMPONENT_UTF16_UNITS = 255
_GIT_COMMIT_SHA_LENGTH = 40
_GIT_COMMIT_SHA_CHARS = frozenset("0123456789abcdef")
_SHA256_LENGTH = 64
_PACKAGE_SNAPSHOT_MEMORY_LIMIT = 8 * 1024 * 1024
# Fail closed on hostile ZIP resource declarations BEFORE payload decompression.
# A legitimate one-file Windows executable fits this bounded release envelope.
_ZIP_MAX_MEMBER_UNCOMPRESSED = 512 * 1024 * 1024
_ZIP_MAX_TOTAL_UNCOMPRESSED = 1024 * 1024 * 1024
_ZIP_MAX_TOTAL_COMPRESSED = 1024 * 1024 * 1024
# A canonical archive has bounded ZIP headers in addition to compressed members.
# Never spool arbitrarily large hostile input before central-directory preflight.
_ZIP_MAX_WIRE_BYTES = _ZIP_MAX_TOTAL_COMPRESSED + 16 * 1024 * 1024
_ZIP_MAX_MEMBERS = 4096
_ZIP_MAX_COMPRESSION_RATIO = 4096
_ZIP_CREATE_SYSTEM = 3
_ZIP_CREATE_VERSION = 20
_ZIP_EXTRACT_VERSION = 20
_ZIP_RESERVED = 0
_ZIP_INTERNAL_ATTR = 0
_ZIP_VOLUME = 0
_ZIP_UTF8_FLAG = 0x800
_ZIP_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_ZIP_LOCAL_HEADER_SIGNATURE = 0x04034B50


class _DuplicateJsonKeyError(ValueError):
    pass


class _NonStandardJsonConstantError(ValueError):
    pass


def _require_git_commit_sha(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _GIT_COMMIT_SHA_LENGTH
        or any(character not in _GIT_COMMIT_SHA_CHARS for character in value)
    ):
        raise ValueError(
            f"{field} must be a canonical 40-character lowercase hexadecimal Git commit SHA"
        )
    return value


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")



def _require_regular_release_input(path: Path, label: str) -> None:
    """Never follow a symlink/device as a release input."""
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"release {label} must be an existing regular file") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"release {label} must be a regular non-symlink file")


def _require_safe_release_example_tree(example_dir: Path) -> None:
    """Screen input tree before copytree can read private or nonregular bytes.

    Source-tree pinning remains the responsibility of the existing exact-Git
    materializer; this boundary prevents direct build callers from silently
    packaging a symlink target or an excluded private/test-state member.
    """
    try:
        root_mode = example_dir.lstat().st_mode
    except OSError as exc:
        raise ValueError("release examples must be an existing directory") from exc
    if not stat.S_ISDIR(root_mode):
        raise ValueError("release examples must be a non-symlink directory")

    seen_windows_paths: set[str] = set()
    root_member = _PACKAGE_PREFIX + "examples/" + example_dir.name
    _, root_key = _validate_windows_member(root_member)
    seen_windows_paths.add(root_key)
    for item in example_dir.rglob("*"):
        try:
            item_mode = item.lstat().st_mode
        except OSError as exc:
            raise ValueError("release example source changed during preflight") from exc
        if not (stat.S_ISDIR(item_mode) or stat.S_ISREG(item_mode)):
            raise ValueError("release examples contain a symlink or nonregular source")
        member = root_member + "/" + item.relative_to(example_dir).as_posix()
        _, windows_key = _validate_windows_member(member)
        if windows_key in seen_windows_paths:
            raise ValueError("release examples contain a Windows path collision")
        seen_windows_paths.add(windows_key)


def _require_safe_release_inputs(
    exe_path: Path, start_file: Path, example_dir: Path,
    diagnostic_path: Path, accessibility_path: Path, keyboard_path: Path,
    restart_recovery_path: Path,
) -> None:
    for label, path in (
        ("executable", exe_path),
        ("start guide", start_file),
        ("diagnostics", diagnostic_path),
        ("accessibility evidence", accessibility_path),
        ("keyboard evidence", keyboard_path),
        ("restart evidence", restart_recovery_path),
    ):
        _require_regular_release_input(path, label)
    _require_safe_release_example_tree(example_dir)

def _sorted_package_files(package_dir: Path) -> list[Path]:
    return sorted(
        (item for item in package_dir.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(package_dir).as_posix(),
    )


def _expected_zip_flag_bits(filename: str) -> int:
    try:
        filename.encode("ascii")
    except UnicodeEncodeError:
        return _ZIP_UTF8_FLAG
    return 0


def _write_canonical_zip(package_zip: Path, members: dict[str, bytes]) -> str:
    """Atomically publish one canonical ZIP and return the SHA of its authored bytes.

    Archive construction happens in a private seekable stream. The digest is
    accumulated while those exact completed bytes are copied to a same-directory
    publication temp, so a later destination-path replacement cannot redefine the
    identity returned by the writer.
    """

    package_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.SpooledTemporaryFile(
        max_size=_PACKAGE_SNAPSHOT_MEMORY_LIMIT,
        mode="w+b",
    ) as authored:
        with zipfile.ZipFile(
            authored,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for archive_name in sorted(members):
                info = zipfile.ZipInfo(archive_name, _FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = _ZIP_CREATE_SYSTEM
                info.create_version = _ZIP_CREATE_VERSION
                info.extract_version = _ZIP_EXTRACT_VERSION
                info.reserved = _ZIP_RESERVED
                info.flag_bits = 0
                info.volume = _ZIP_VOLUME
                info.internal_attr = _ZIP_INTERNAL_ATTR
                info.external_attr = (
                    0o755 if archive_name.lower().endswith(".exe") else 0o644
                ) << 16
                archive.writestr(
                    info,
                    members[archive_name],
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )

        # A writer must not publish a ZIP that the canonical verifier rejects.
        # Inspect the completed central directory while output is still private;
        # no caller-visible archive exists if the resource contract fails.
        authored.seek(0)
        with zipfile.ZipFile(authored, "r") as completed_archive:
            _require_release_zip_resource_bounds(completed_archive.infolist())
        authored.seek(0)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{package_zip.name}.",
            suffix=".tmp",
            dir=package_zip.parent,
        )
        publication = Path(tmp_name)
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in iter(lambda: authored.read(1024 * 1024), b""):
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            writer_sha = digest.hexdigest()
            os.replace(publication, package_zip)
            return writer_sha
        finally:
            if publication.exists():
                publication.unlink()


def _require_release_zip_resource_bounds(infos: list[zipfile.ZipInfo]) -> None:
    """Screen all central-directory sizes before reading even one member.

    Trust no advertised size as a license for unbounded decompression. The
    independent content hashes/local-header checks still validate identities.
    """
    if len(infos) > _ZIP_MAX_MEMBERS:
        raise ValueError("release ZIP resource limit: excessive member count")
    total_uncompressed = 0
    total_compressed = 0
    for info in infos:
        expanded = info.file_size
        packed = info.compress_size
        if expanded < 0 or packed < 0 or expanded > _ZIP_MAX_MEMBER_UNCOMPRESSED:
            raise ValueError("release ZIP resource limit: unsafe member size")
        if expanded and (packed == 0 or expanded > packed * _ZIP_MAX_COMPRESSION_RATIO):
            raise ValueError("release ZIP resource limit: excessive compression ratio")
        total_uncompressed += expanded
        total_compressed += packed
        if (total_uncompressed > _ZIP_MAX_TOTAL_UNCOMPRESSED or
                total_compressed > _ZIP_MAX_TOTAL_COMPRESSED):
            raise ValueError("release ZIP resource limit: excessive archive size")


def _require_canonical_zip_metadata(
    infos: list[zipfile.ZipInfo],
    archive_comment: bytes,
) -> None:
    if archive_comment:
        raise ValueError("release package contains a non-canonical archive comment")

    names = [info.filename for info in infos]
    if names != sorted(names):
        raise ValueError("release package member order is not canonical")

    for info in infos:
        if info.date_time != _FIXED_ZIP_TIME:
            raise ValueError(
                f"release package member has non-canonical timestamp: {info.filename}"
            )
        if info.compress_type != zipfile.ZIP_DEFLATED:
            raise ValueError(
                f"release package member has non-canonical compression method: {info.filename}"
            )
        if info.create_system != _ZIP_CREATE_SYSTEM:
            raise ValueError(
                f"release package member has non-canonical create system: {info.filename}"
            )
        if info.create_version != _ZIP_CREATE_VERSION:
            raise ValueError(
                f"release package member has non-canonical create version: {info.filename}"
            )
        if info.extract_version != _ZIP_EXTRACT_VERSION:
            raise ValueError(
                f"release package member has non-canonical extract version: {info.filename}"
            )
        if info.reserved != _ZIP_RESERVED:
            raise ValueError(
                f"release package member has non-canonical reserved metadata: {info.filename}"
            )
        expected_flags = _expected_zip_flag_bits(info.filename)
        if info.flag_bits != expected_flags:
            raise ValueError(
                f"release package member has non-canonical flag bits: {info.filename}"
            )
        if info.volume != _ZIP_VOLUME:
            raise ValueError(
                f"release package member has non-canonical volume metadata: {info.filename}"
            )
        if info.internal_attr != _ZIP_INTERNAL_ATTR:
            raise ValueError(
                f"release package member has non-canonical internal attributes: {info.filename}"
            )
        expected_mode = 0o755 if info.filename.lower().endswith(".exe") else 0o644
        if info.external_attr != expected_mode << 16:
            raise ValueError(
                f"release package member has non-canonical permissions: {info.filename}"
            )
        if info.extra or info.comment:
            raise ValueError(
                f"release package member has non-canonical member metadata: {info.filename}"
            )


def _require_canonical_zip_local_headers(
    snapshot: Any,
    infos: list[zipfile.ZipInfo],
) -> None:
    """Require canonical local records and a byte-exhaustive ZIP structure."""

    original_position = snapshot.tell()
    try:
        expected_local_offset = 0
        for info in infos:
            if info.header_offset != expected_local_offset:
                raise ValueError(
                    "release package contains unclaimed bytes before local header: "
                    f"{info.filename}"
                )

            snapshot.seek(info.header_offset)
            raw_header = snapshot.read(_ZIP_LOCAL_HEADER.size)
            if len(raw_header) != _ZIP_LOCAL_HEADER.size:
                raise ValueError(
                    f"release package has truncated local header: {info.filename}"
                )
            (
                signature,
                extract_version,
                flag_bits,
                compress_type,
                dos_time,
                dos_date,
                crc,
                compress_size,
                file_size,
                filename_length,
                extra_length,
            ) = _ZIP_LOCAL_HEADER.unpack(raw_header)
            if signature != _ZIP_LOCAL_HEADER_SIGNATURE:
                raise ValueError(
                    f"release package has invalid local header signature: {info.filename}"
                )

            raw_filename = snapshot.read(filename_length)
            local_extra = snapshot.read(extra_length)
            if len(raw_filename) != filename_length or len(local_extra) != extra_length:
                raise ValueError(
                    f"release package has truncated local header metadata: {info.filename}"
                )

            encoding = "utf-8" if info.flag_bits & _ZIP_UTF8_FLAG else "cp437"
            try:
                expected_filename = info.filename.encode(encoding)
            except UnicodeEncodeError as exc:
                raise ValueError(
                    f"release package local header filename encoding mismatch: {info.filename}"
                ) from exc

            year, month, day, hour, minute, second = info.date_time
            expected_dos_time = (hour << 11) | (minute << 5) | (second // 2)
            expected_dos_date = ((year - 1980) << 9) | (month << 5) | day
            expected_header = (
                info.extract_version,
                info.flag_bits,
                info.compress_type,
                expected_dos_time,
                expected_dos_date,
                info.CRC,
                info.compress_size,
                info.file_size,
                len(expected_filename),
                len(info.extra),
            )
            observed_header = (
                extract_version,
                flag_bits,
                compress_type,
                dos_time,
                dos_date,
                crc,
                compress_size,
                file_size,
                filename_length,
                extra_length,
            )
            if (
                observed_header != expected_header
                or raw_filename != expected_filename
                or local_extra != info.extra
            ):
                raise ValueError(
                    "release package local header metadata does not match canonical "
                    f"central metadata: {info.filename}"
                )

            expected_local_offset = snapshot.tell() + info.compress_size

        eocd = struct.Struct("<IHHHHIIH")
        snapshot.seek(0, os.SEEK_END)
        snapshot_size = snapshot.tell()
        eocd_offset = snapshot_size - eocd.size
        if eocd_offset < expected_local_offset:
            raise ValueError("release package end-of-central-directory is truncated")
        snapshot.seek(eocd_offset)
        raw_eocd = snapshot.read(eocd.size)
        if len(raw_eocd) != eocd.size:
            raise ValueError("release package end-of-central-directory is truncated")
        (
            eocd_signature,
            disk_number,
            central_directory_disk,
            entries_on_disk,
            entries_total,
            central_directory_size,
            central_directory_offset,
            comment_length,
        ) = eocd.unpack(raw_eocd)
        if (
            eocd_signature != 0x06054B50
            or disk_number != 0
            or central_directory_disk != 0
            or entries_on_disk != len(infos)
            or entries_total != len(infos)
            or comment_length != 0
        ):
            raise ValueError(
                "release package end-of-central-directory is not canonical"
            )
        if central_directory_offset != expected_local_offset:
            raise ValueError(
                "release package contains unclaimed bytes between local records "
                "and central directory"
            )
        if central_directory_offset + central_directory_size != eocd_offset:
            raise ValueError(
                "release package central-directory span is not canonical"
            )
    finally:
        snapshot.seek(original_position)


def _require_process_recovery_evidence(payload: dict[str, Any], label: str) -> None:
    if payload.get("process_kill_relaunch_status") != "PASS":
        raise ValueError(f"{label} does not prove real process kill/relaunch PASS")

    process_ids: dict[str, int] = {}
    for field in ("process_kill_stage_pid", "process_recovery_pid"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} has invalid {field}")
        process_ids[field] = value
    if process_ids["process_kill_stage_pid"] == process_ids["process_recovery_pid"]:
        raise ValueError(f"{label} does not prove a distinct fresh recovery process")

    killed_return_code = payload.get("process_kill_return_code")
    if (
        isinstance(killed_return_code, bool)
        or not isinstance(killed_return_code, int)
        or killed_return_code == 0
    ):
        raise ValueError(f"{label} does not prove non-clean process termination")

    run_id = payload.get("process_recovery_run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError(f"{label} has invalid process_recovery_run_id")

    required_states = {
        "process_recovery_disposition": "committed",
        "process_recovery_registry_status": "completed",
        "process_recovery_manifest_phase": "completed",
    }
    for field, expected in required_states.items():
        if payload.get(field) != expected:
            raise ValueError(f"{label} does not prove {field}={expected}")

    hash_fields = (
        "process_recovery_base_paper_book_sha256",
        "process_recovery_base_decision_ledger_sha256",
        "process_recovery_new_paper_book_sha256",
        "process_recovery_new_decision_ledger_sha256",
    )
    hashes: dict[str, str] = {}
    for field in hash_fields:
        value = payload.get(field)
        if (
            not isinstance(value, str)
            or len(value) != _SHA256_LENGTH
            or any(character not in _GIT_COMMIT_SHA_CHARS for character in value)
        ):
            raise ValueError(f"{label} has invalid {field}")
        hashes[field] = value

    if (
        hashes["process_recovery_base_paper_book_sha256"]
        == hashes["process_recovery_new_paper_book_sha256"]
    ):
        raise ValueError(f"{label} does not prove promoted PaperBook state")
    if (
        hashes["process_recovery_base_decision_ledger_sha256"]
        == hashes["process_recovery_new_decision_ledger_sha256"]
    ):
        raise ValueError(f"{label} does not prove promoted Decision Ledger state")


# This records immutable source declarations only. It is not an installed-wheel
# SBOM, signing proof or claim that third-party licenses were accepted.
_SOURCE_MANIFEST_INPUTS = frozenset({
    "pyproject.toml",
    "scripts/package_windows.py",
    "src/autosport/release_package.py",
    "src/autosport/release_sbom.py",
    # Bind the shipped operator configuration and durable on-disk schema
    # authorities to the same immutable exact-Git release source declaration.
    "src/autosport/windows_entry.py",
    "src/autosport/windows_layout.py",
    "src/autosport/paths.py",
    "src/autosport/storage.py",
    "src/autosport/workspace_lock.py",
    "src/autosport/monotonic_workspace_authority.py",
    "src/autosport/release_dependency_graph.py",
    "src/autosport/windows_pe_integrity.py",
    "scripts/build_windows.ps1",
    "scripts/build_windows_candidate.ps1",
    ".github/workflows/windows-build.yml",
})


def _validate_source_manifest(value: dict[str, Any], source_sha: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "source_sha", "evidence_class",
        "source_files_sha256", "declared_runtime_dependencies",
        "declared_build_dependencies",
    }:
        raise ValueError("SOURCE_MANIFEST schema is invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("SOURCE_MANIFEST schema is invalid")
    if value["source_sha"] != source_sha:
        raise ValueError("SOURCE_MANIFEST source SHA does not match BUILD_INFO")
    if value["evidence_class"] != "SOURCE_DECLARATIONS_ONLY":
        raise ValueError("SOURCE_MANIFEST evidence scope is invalid")
    source_files = value["source_files_sha256"]
    if not isinstance(source_files, dict) or set(source_files) != _SOURCE_MANIFEST_INPUTS:
        raise ValueError("SOURCE_MANIFEST source input set is invalid")
    for digest in source_files.values():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("SOURCE_MANIFEST source digest is invalid")
    # Distribution identities follow PEP 503 normalization, not spelling:
    # tk-uia, tk_uia and TK.UIA all address the same installed distribution.
    # Recomputable ZIP hashes must not disguise aliases or conflicting pins.
    pinned_versions: dict[str, str] = {}
    for field in ("declared_runtime_dependencies", "declared_build_dependencies"):
        dependencies = value[field]
        if not isinstance(dependencies, list) or not dependencies:
            raise ValueError("SOURCE_MANIFEST dependency declaration is missing")
        if dependencies != sorted(set(dependencies)):
            raise ValueError("SOURCE_MANIFEST dependency declaration is not unique/sorted")
        names_in_scope: set[str] = set()
        for requirement in dependencies:
            match = (
                re.fullmatch(
                    r"([A-Za-z][A-Za-z0-9_.-]*)==([0-9][A-Za-z0-9_.+-]*)",
                    requirement,
                )
                if isinstance(requirement, str)
                else None
            )
            if match is None:
                raise ValueError("SOURCE_MANIFEST dependency is not exactly pinned")
            package_name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            version = match.group(2)
            if package_name in names_in_scope:
                raise ValueError("SOURCE_MANIFEST contains normalized dependency aliases")
            names_in_scope.add(package_name)
            previous_version = pinned_versions.get(package_name)
            if previous_version is not None and previous_version != version:
                raise ValueError("SOURCE_MANIFEST contains conflicting dependency versions")
            pinned_versions[package_name] = version



def _release_source_fingerprint(info: os.stat_result) -> tuple[int, ...]:
    """File identity and mutation witness, excluding access-time-only changes."""
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def _require_release_source_tree_without_links(root: Path) -> dict[str, tuple[int, ...]]:
    """Return a stable relative-path witness for a regular source tree.

    Source files are subsequently copied using pinned descriptors, not the
    follow-symlinks behavior of shutil.copytree. The entire witness is checked
    again after staging to detect changed names, directories or file metadata.
    """
    entries: dict[str, tuple[int, ...]] = {}

    def check(path: Path, *, directory: bool) -> None:
        info = path.lstat()
        junction = getattr(path, "is_junction", None)
        if (junction is not None and junction()) or not (
            stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        ):
            raise ValueError("unsafe release source link or special file")
        relative = "." if path == root else path.relative_to(root).as_posix()
        entries[relative] = _release_source_fingerprint(info)

    check(root, directory=True)
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        check(base, directory=True)
        for name in sorted(dirs):
            check(base / name, directory=True)
        for name in sorted(files):
            check(base / name, directory=False)
    return entries


def _require_release_regular_source(path: Path) -> None:
    mode = path.lstat().st_mode
    junction = getattr(path, "is_junction", None)
    if not stat.S_ISREG(mode) or (junction is not None and junction()):
        raise ValueError("unsafe release source link or special file")


def _copy_release_regular_source(
    source: Path, destination: Path, *,
    expected_fingerprint: tuple[int, ...] | None = None,
) -> None:
    """Copy pinned regular-file bytes and reject source mutation during read.

    The initial source-tree fingerprint also prevents accepting a different
    regular inode substituted between inventory and copy. The opened descriptor
    must agree with the source path before and after the copy.
    """
    try:
        fd = os.open(
            source, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError:
        raise ValueError("unsafe release source open") from None
    try:
        opened = os.fstat(fd)
        current = source.lstat()
        junction = getattr(source, "is_junction", None)
        initial = _release_source_fingerprint(opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (junction is not None and junction())
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or (expected_fingerprint is not None and initial != expected_fingerprint)
        ):
            raise ValueError("unsafe release source identity changed")
        with os.fdopen(fd, "rb") as stream:
            fd = -1  # fdopen owns the descriptor now
            with destination.open("wb") as output:
                shutil.copyfileobj(stream, output)
            # Check while the pinned handle is still alive. A path replacement,
            # truncate, or in-place rewrite must not publish mixed bytes.
            final_opened = os.fstat(stream.fileno())
            final_path = source.lstat()
            junction = getattr(source, "is_junction", None)
            if (
                (junction is not None and junction())
                or not stat.S_ISREG(final_path.st_mode)
                or _release_source_fingerprint(final_opened) != initial
                or _release_source_fingerprint(final_path) != initial
            ):
                raise ValueError("unsafe release source changed during copy")
    finally:
        if fd >= 0:
            os.close(fd)


def _copy_release_example_tree(
    source: Path, destination: Path,
    witness: dict[str, tuple[int, ...]],
) -> None:
    """Stage examples without ever dereferencing a source-file symlink.

    Validate member names before reading potentially private source bytes.
    The final complete source inventory must match the original one.
    """
    destination.mkdir(parents=True)
    for relative in sorted(witness, key=lambda name: (name.count("/"), name)):
        if relative == ".":
            continue
        source_path = source / relative
        target = destination / relative
        member = (Path("Autosport-V1") / "examples" / source.name / relative).as_posix()
        _validate_windows_member(member)
        info = source_path.lstat()
        if _release_source_fingerprint(info) != witness[relative]:
            raise ValueError("unsafe release source changed during staging")
        if stat.S_ISDIR(info.st_mode):
            target.mkdir()
        else:
            _copy_release_regular_source(
                source_path, target, expected_fingerprint=witness[relative],
            )
    if _require_release_source_tree_without_links(source) != witness:
        raise ValueError("unsafe release source tree changed during staging")


def _require_dependency_evidence_coherence(
    direct_metadata: dict[str, Any],
    dependency_graph: dict[str, Any],
) -> None:
    """Reject contradictory claims about the same build-host distribution.

    Both evidence objects must already have passed their separate strict
    source-bound schema validators. This check does NOT elevate either
    build-host inventory into a bundled executable SBOM or license clearance.
    """
    graph_by_name = {
        component["name"]: component
        for component in dependency_graph["components"]
    }
    for direct in direct_metadata["distributions"]:
        component = graph_by_name.get(direct["name"])
        if component is None or (
            component["version"] != direct["version"]
            or component["direct_roles"] != direct["roles"]
            or component["license_expression"] != direct["license_expression"]
            or component["license_evidence"] != direct["license_evidence"]
        ):
            raise ValueError("direct and transitive build-host evidence disagree")


def build_windows_package(
    exe_path: str | Path,
    start_file: str | Path,
    example_dir: str | Path,
    diagnostic_path: str | Path,
    accessibility_path: str | Path,
    keyboard_path: str | Path,
    restart_recovery_path: str | Path,
    output_zip: str | Path,
    source_sha: str,
    *,
    source_manifest: dict[str, Any] | None = None,
    dependency_metadata: dict[str, Any] | None = None,
    dependency_graph: dict[str, Any] | None = None,
    update_version: str | None = None,
    update_schema: int | None = None,
) -> tuple[Path, str]:
    _require_git_commit_sha(source_sha, field="source_sha")
    if source_manifest is not None:
        _validate_source_manifest(source_manifest, source_sha)
    if dependency_metadata is not None:
        if source_manifest is None:
            raise ValueError("dependency metadata requires source declarations")
        validate_direct_dependency_metadata(dependency_metadata, source_manifest)
    if dependency_graph is not None:
        if source_manifest is None:
            raise ValueError("build-host graph requires source declarations")
        validate_build_host_dependency_graph(dependency_graph, source_manifest)
    if dependency_metadata is not None and dependency_graph is not None:
        _require_dependency_evidence_coherence(dependency_metadata, dependency_graph)
    validate_requested_update_identity(
        update_version=update_version, update_schema=update_schema
    )
    exe_path = Path(exe_path)
    start_file = Path(start_file)
    example_dir = Path(example_dir)
    diagnostic_path = Path(diagnostic_path)
    accessibility_path = Path(accessibility_path)
    keyboard_path = Path(keyboard_path)
    restart_recovery_path = Path(restart_recovery_path)
    output_zip = Path(output_zip)
    _require_safe_release_inputs(
        exe_path, start_file, example_dir, diagnostic_path,
        accessibility_path, keyboard_path, restart_recovery_path,
    )
    for source in (
        exe_path, start_file, diagnostic_path, accessibility_path,
        keyboard_path, restart_recovery_path,
    ):
        _require_release_regular_source(source)
    example_witness = _require_release_source_tree_without_links(example_dir)
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".autosport-release-stage-",
        dir=output_zip.parent,
    ) as staging_root:
        package_dir = Path(staging_root) / "Autosport-V1"
        package_dir.mkdir()
        _copy_release_regular_source(exe_path, package_dir / "Autosport.exe")
        _copy_release_regular_source(start_file, package_dir / "WINDOWS_START_HERE.txt")
        _copy_release_regular_source(diagnostic_path, package_dir / "packaged-diagnostic.json")
        _copy_release_regular_source(accessibility_path, package_dir / "accessibility-audit.json")
        _copy_release_regular_source(keyboard_path, package_dir / "keyboard-audit.json")
        _copy_release_regular_source(restart_recovery_path, package_dir / "restart-recovery-audit.json")
        _copy_release_example_tree(
            example_dir, package_dir / "examples" / example_dir.name, example_witness,
        )
        _require_release_source_tree_without_links(package_dir)

        build_info = {
            "product": "Autosport",
            "version": _PREHUMAN_BUILD_VERSION,
            "source_sha": source_sha,
            "autosport_exe_sha256": sha256_file(package_dir / "Autosport.exe"),
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        build_info = attach_update_identity(
            build_info, update_version=update_version, update_schema=update_schema
        )
        _write_json(package_dir / "BUILD_INFO.json", build_info)
        if source_manifest is not None:
            _write_json(package_dir / "SOURCE_MANIFEST.json", source_manifest)
        if dependency_metadata is not None:
            _write_json(package_dir / "DIRECT_DEPENDENCY_METADATA.json", dependency_metadata)
        if dependency_graph is not None:
            _write_json(package_dir / "DEPENDENCY_GRAPH_METADATA.json", dependency_graph)

        payload_hashes = {}
        for path in _sorted_package_files(package_dir):
            relative = path.relative_to(package_dir).as_posix()
            if relative == "SHA256SUMS.txt":
                continue
            payload_hashes[relative] = sha256_file(path)
        _write_json(
            package_dir / "PACKAGE_MANIFEST.json",
            {"schema_version": 1, "files": payload_hashes},
        )

        lines = []
        for path in _sorted_package_files(package_dir):
            relative = path.relative_to(package_dir).as_posix()
            if relative == "SHA256SUMS.txt":
                continue
            lines.append(f"{sha256_file(path)}  {relative}")
        (package_dir / "SHA256SUMS.txt").write_bytes(
            ("\n".join(lines) + "\n").encode("utf-8")
        )

        archive_members: dict[str, bytes] = {}
        for path in _sorted_package_files(package_dir):
            name = (Path("Autosport-V1") / path.relative_to(package_dir)).as_posix()
            _validate_windows_member(name)  # reject private/test state before publication
            archive_members[name] = path.read_bytes()
        writer_sha = _write_canonical_zip(output_zip, archive_members)
        return output_zip, writer_sha


def verify_windows_package(
    package_zip: str | Path,
    *,
    expected_source_sha: str,
    require_source_manifest: bool = False,
    require_dependency_metadata: bool = False,
    require_dependency_graph: bool = False,
) -> dict[str, Any]:
    """Fail closed on release ZIP identity, canonical metadata, truth labels, or payload drift."""

    _require_git_commit_sha(expected_source_sha, field="expected_source_sha")
    package_zip = Path(package_zip)
    digest = hashlib.sha256()
    with package_zip.open("rb") as source, tempfile.SpooledTemporaryFile(
        max_size=_PACKAGE_SNAPSHOT_MEMORY_LIMIT,
        mode="w+b",
    ) as snapshot:
        wire_bytes = 0
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            wire_bytes += len(chunk)
            if wire_bytes > _ZIP_MAX_WIRE_BYTES:
                raise ValueError("release ZIP wire byte limit exceeded before snapshot publication")
            digest.update(chunk)
            snapshot.write(chunk)
        package_sha = digest.hexdigest()
        snapshot.seek(0)
        with zipfile.ZipFile(snapshot, "r") as archive:
            infos = archive.infolist()
            _require_release_zip_resource_bounds(infos)
            archive_comment = archive.comment
            directory_names = [item.filename for item in infos if item.is_dir()]
            if directory_names:
                raise ValueError(
                    "release package contains unsupported directory entries: "
                    + ", ".join(directory_names)
                )
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise ValueError("release package contains duplicate member names")
            members: dict[str, bytes] = {}
            windows_keys: dict[str, str] = {}
            for name in names:
                relative, windows_key = _validate_windows_member(name)
                previous = windows_keys.get(windows_key)
                if previous is not None:
                    raise ValueError(
                        "release package contains Windows path collision: "
                        f"{previous} vs {name}"
                    )
                windows_keys[windows_key] = name
                members[relative] = archive.read(name)
            _require_canonical_zip_local_headers(snapshot, infos)

    required = {
        "Autosport.exe",
        "WINDOWS_START_HERE.txt",
        "packaged-diagnostic.json",
        "accessibility-audit.json",
        "keyboard-audit.json",
        "restart-recovery-audit.json",
        "BUILD_INFO.json",
        "PACKAGE_MANIFEST.json",
        "SHA256SUMS.txt",
    }
    missing = sorted(required.difference(members))
    if missing:
        raise ValueError("release package is missing required files: " + ", ".join(missing))

    build_info = _decode_json_object(members["BUILD_INFO.json"], "BUILD_INFO.json")
    if build_info.get("product") != "Autosport":
        raise ValueError("BUILD_INFO product identity mismatch")
    # Preserve upstream strict prehuman identity for legacy packages. Explicit
    # update packages use their separately validated canonical semantic version.
    if "update_version" not in build_info and "update_schema" not in build_info:
        if type(build_info.get("version")) is not str or build_info["version"] != _PREHUMAN_BUILD_VERSION:
            raise ValueError("BUILD_INFO unsupported or ambiguous prehuman version")
    if build_info.get("source_sha") != expected_source_sha:
        raise ValueError("BUILD_INFO source_sha does not match exact candidate head")
    validate_embedded_update_identity(build_info)
    _require_false_truth_labels(build_info, "BUILD_INFO.json")
    if require_source_manifest and "SOURCE_MANIFEST.json" not in members:
        raise ValueError("release package is missing SOURCE_MANIFEST.json")
    if "SOURCE_MANIFEST.json" in members:
        source_manifest = _decode_json_object(members["SOURCE_MANIFEST.json"], "SOURCE_MANIFEST.json")
        _validate_source_manifest(source_manifest, expected_source_sha)
        if members["SOURCE_MANIFEST.json"] != _canonical_json_bytes(source_manifest):
            raise ValueError("SOURCE_MANIFEST is not in canonical JSON representation")
    if require_dependency_metadata and "DIRECT_DEPENDENCY_METADATA.json" not in members:
        raise ValueError("release package is missing direct dependency metadata")
    if "DIRECT_DEPENDENCY_METADATA.json" in members:
        if "SOURCE_MANIFEST.json" not in members:
            raise ValueError("direct dependency metadata has no source declarations")
        direct_metadata = _decode_json_object(
            members["DIRECT_DEPENDENCY_METADATA.json"], "DIRECT_DEPENDENCY_METADATA.json"
        )
        validate_direct_dependency_metadata(direct_metadata, source_manifest)
        if members["DIRECT_DEPENDENCY_METADATA.json"] != _canonical_json_bytes(direct_metadata):
            raise ValueError("direct dependency metadata is not canonical JSON")
    if require_dependency_graph and "DEPENDENCY_GRAPH_METADATA.json" not in members:
        raise ValueError("release package is missing build-host dependency graph")
    if "DEPENDENCY_GRAPH_METADATA.json" in members:
        if "SOURCE_MANIFEST.json" not in members:
            raise ValueError("build-host dependency graph has no source declarations")
        graph = _decode_json_object(
            members["DEPENDENCY_GRAPH_METADATA.json"], "DEPENDENCY_GRAPH_METADATA.json"
        )
        validate_build_host_dependency_graph(graph, source_manifest)
        if members["DEPENDENCY_GRAPH_METADATA.json"] != _canonical_json_bytes(graph):
            raise ValueError("build-host dependency graph is not canonical JSON")
    if ("DIRECT_DEPENDENCY_METADATA.json" in members
            and "DEPENDENCY_GRAPH_METADATA.json" in members):
        _require_dependency_evidence_coherence(direct_metadata, graph)
    exe_sha = _sha256_bytes(members["Autosport.exe"])
    if build_info.get("autosport_exe_sha256") != exe_sha:
        raise ValueError("BUILD_INFO Autosport.exe hash mismatch")

    manifest = _decode_json_object(
        members["PACKAGE_MANIFEST.json"],
        "PACKAGE_MANIFEST.json",
    )
    schema_version = manifest.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
        or not isinstance(manifest.get("files"), dict)
    ):
        raise ValueError("PACKAGE_MANIFEST schema is invalid")
    manifest_files = {
        str(key): str(value)
        for key, value in manifest["files"].items()
    }
    expected_manifest_files = set(members).difference(
        {"PACKAGE_MANIFEST.json", "SHA256SUMS.txt"}
    )
    if set(manifest_files) != expected_manifest_files:
        raise ValueError("PACKAGE_MANIFEST file set does not match package payload")
    for relative, expected_hash in manifest_files.items():
        if _sha256_bytes(members[relative]) != expected_hash:
            raise ValueError(f"PACKAGE_MANIFEST hash mismatch: {relative}")

    sums: dict[str, str] = {}
    for line in members["SHA256SUMS.txt"].decode("utf-8").splitlines():
        digest_value, separator, relative = line.partition("  ")
        if (
            not separator
            or len(digest_value) != 64
            or not relative
            or relative in sums
        ):
            raise ValueError("SHA256SUMS contains a malformed or duplicate entry")
        try:
            int(digest_value, 16)
        except ValueError as exc:
            raise ValueError("SHA256SUMS contains a non-hex digest") from exc
        sums[relative] = digest_value.lower()
    expected_sum_files = set(members).difference({"SHA256SUMS.txt"})
    if set(sums) != expected_sum_files:
        raise ValueError("SHA256SUMS file set does not match package payload")
    for relative, expected_hash in sums.items():
        if _sha256_bytes(members[relative]) != expected_hash:
            raise ValueError(f"SHA256SUMS hash mismatch: {relative}")

    diagnostic = _decode_json_object(
        members["packaged-diagnostic.json"],
        "packaged-diagnostic.json",
    )
    accessibility = _decode_json_object(
        members["accessibility-audit.json"],
        "accessibility-audit.json",
    )
    keyboard = _decode_json_object(
        members["keyboard-audit.json"],
        "keyboard-audit.json",
    )
    restart_recovery = _decode_json_object(
        members["restart-recovery-audit.json"],
        "restart-recovery-audit.json",
    )
    for label, payload in (
        ("packaged-diagnostic.json", diagnostic),
        ("accessibility-audit.json", accessibility),
        ("keyboard-audit.json", keyboard),
        ("restart-recovery-audit.json", restart_recovery),
    ):
        if payload.get("status") != "PASS":
            raise ValueError(f"{label} does not record PASS")
        _require_false_truth_labels(payload, label)

    if restart_recovery.get("session_restart_status") != "PASS":
        raise ValueError("restart-recovery-audit.json does not prove session restart PASS")
    if restart_recovery.get("transaction_recovery_status") != "PASS":
        raise ValueError("restart-recovery-audit.json does not prove transaction recovery PASS")
    if restart_recovery.get("recovery_disposition") != "aborted_uncommitted":
        raise ValueError("restart-recovery-audit.json recovery disposition is not fail-closed")
    _require_process_recovery_evidence(
        restart_recovery,
        "restart-recovery-audit.json",
    )

    if members["BUILD_INFO.json"] != _canonical_json_bytes(build_info):
        raise ValueError("BUILD_INFO.json is not in canonical JSON representation")
    if members["PACKAGE_MANIFEST.json"] != _canonical_json_bytes(manifest):
        raise ValueError("PACKAGE_MANIFEST.json is not in canonical JSON representation")
    canonical_sums = "".join(
        f"{sums[relative]}  {relative}\n"
        for relative in sorted(sums)
    ).encode("utf-8")
    if members["SHA256SUMS.txt"] != canonical_sums:
        raise ValueError("SHA256SUMS.txt is not in canonical sorted representation")
    _require_canonical_zip_metadata(infos, archive_comment)

    return {
        "status": "PASS",
        "source_sha": expected_source_sha,
        "package_sha256": package_sha,
        "autosport_exe_sha256": exe_sha,
        "file_count": len(members),
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }


_RELEASE_PRIVATE_OR_TEST_DIRECTORIES = frozenset({
    ".aws", ".azure", ".mozilla", ".ssh", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", "__pycache__", "user data",
})
_RELEASE_PRIVATE_OR_TEST_FILES = frozenset({
    ".env", ".envrc", ".pypirc", "application_default_credentials.json",
    "cookies", "cookies.json", "cookies.sqlite", "credentials",
    "credentials.json", "key3.db", "key4.db", "local state",
    "login data", "logins.json", "oauth_credentials.json",
    "secrets.json", "secrets.toml", "token.json", "web data",
    ".coverage", "coverage.xml", ".testmondata", "pytestdebug.log",
    "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
})
_RELEASE_SAFE_ENV_TEMPLATES = frozenset({
    ".env.example", ".env.sample", ".env.template", ".env.dist",
})
_RELEASE_PRIVATE_KEY_STEMS = frozenset({
    "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
})


def _require_release_member_without_private_or_test_state(
    relative_path: PurePosixPath,
) -> None:
    """Reject forbidden package bytes even if a ZIP's hashes were recomputed.

    Build-time Git-tree screening is not sufficient: the verifier is also used
    on externally supplied and repacked archives. Never trust a self-consistent
    manifest to authorize cached tests or local credential material.
    """
    components = tuple(part.casefold() for part in relative_path.parts)
    if any(part in _RELEASE_PRIVATE_OR_TEST_DIRECTORIES for part in components[:-1]):
        raise ValueError("release package contains forbidden private/test state directory")
    leaf = components[-1]
    if (
        leaf in _RELEASE_PRIVATE_OR_TEST_FILES
        or (leaf.startswith(".env.") and leaf not in _RELEASE_SAFE_ENV_TEMPLATES)
        or (leaf.startswith("client_secret") and leaf.endswith(".json"))
        or leaf.startswith(".coverage.")
        or leaf.endswith((".pyc", ".pyo", ".pfx", ".p12", ".ppk", ".key"))
        or leaf.endswith((".session", ".session-journal"))
        or (
            leaf.endswith(".pem")
            and ("private" in leaf or leaf[:-4] in _RELEASE_PRIVATE_KEY_STEMS)
        )
    ):
        raise ValueError("release package contains forbidden private/test state file")


def _validate_windows_member(name: str) -> tuple[str, str]:
    """Return canonical relative/member key or reject a path unsafe for Windows extraction."""

    if "\\" in name:
        raise ValueError(f"release package contains Windows backslash member: {name}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or not name.startswith(_PACKAGE_PREFIX):
        raise ValueError(f"release package contains unsafe member: {name}")
    relative = name[len(_PACKAGE_PREFIX):]
    if not relative:
        raise ValueError("release package contains an empty member name")
    relative_path = PurePosixPath(relative)
    canonical = "/".join(relative_path.parts)
    if relative != canonical or not relative_path.parts:
        raise ValueError(f"release package contains non-canonical member path: {name}")

    normalized_parts: list[str] = []
    for component in relative_path.parts:
        if component in {"", ".", ".."}:
            raise ValueError(f"release package contains unsafe Windows path component: {name}")
        if component[-1] in {" ", "."}:
            raise ValueError(f"release package contains Windows trailing space or dot: {name}")
        if any(
            ord(character) < 32 or character in _WINDOWS_INVALID_CHARS
            for character in component
        ):
            raise ValueError(f"release package contains Windows-invalid path character: {name}")
        if len(component.encode("utf-16-le")) // 2 > _WINDOWS_MAX_COMPONENT_UTF16_UNITS:
            raise ValueError(f"release package contains overlong Windows path component: {name}")
        device_stem = component.split(".", 1)[0].casefold()
        if device_stem in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"release package contains reserved Windows device name: {name}")
        normalized_parts.append(component.casefold())
    _require_release_member_without_private_or_test_state(relative_path)
    return relative, "/".join(normalized_parts)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKeyError(key)
        value[key] = item
    return value


def _reject_nonstandard_json_constant(value: str) -> None:
    raise _NonStandardJsonConstantError(value)


def _decode_json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except _DuplicateJsonKeyError as exc:
        raise ValueError(
            f"{label} contains duplicate JSON object key: {exc.args[0]}"
        ) from exc
    except _NonStandardJsonConstantError as exc:
        raise ValueError(
            f"{label} contains non-standard JSON constant: {exc.args[0]}"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _require_false_truth_labels(payload: dict[str, Any], label: str) -> None:
    for key in ("real_money_execution", "human_tested", "nvda_verified"):
        if payload.get(key) is not False:
            raise ValueError(f"{label} must record {key}=false")


def _write_json(path: Path, payload: dict) -> None:
    path.write_bytes(_canonical_json_bytes(payload))
