from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_PACKAGE_PREFIX = "Autosport-V1/"
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
_ZIP_CREATE_SYSTEM = 3
_ZIP_CREATE_VERSION = 20
_ZIP_EXTRACT_VERSION = 20
_ZIP_RESERVED = 0
_ZIP_INTERNAL_ATTR = 0
_ZIP_VOLUME = 0
_ZIP_UTF8_FLAG = 0x800
_ZIP_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_ZIP_LOCAL_HEADER_SIGNATURE = 0x04034B50
_SECRET_KEY_PATTERN = (
    rb"(?:x[._ -]?)?(?:"
    rb"api(?:[._ -]?(?:key|secret|token|hash))|"
    rb"access[._ -]?token|refresh[._ -]?token|auth[._ -]?token|bearer[._ -]?token|"
    rb"oauth2?(?:[._ -]?(?:(?:access|refresh)[._ -]?token|token|secret))|"
    rb"client[._ -]?secret|consumer[._ -]?secret|"
    rb"session(?:[._ -]?(?:token|id))|bot[._ -]?token|password|passwd|authorization|"
    rb"cookie|set[._ -]?cookie"
    rb")"
)
_SECRET_KEY_FULL_PATTERN = re.compile(
    rb"^(?:" + _SECRET_KEY_PATTERN + rb")$",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    rb"(?ix)"
    rb"(?:^|[,{;&#|\t ]|//)"
    rb"(?P<keyquote>[\"']?)"
    rb"(?P<key>" + _SECRET_KEY_PATTERN + rb")"
    rb"(?P=keyquote)"
    rb"[\t ]*[:=][\t ]*"
    rb"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\t \r\n,;]+)"
)
_SECRET_AUTH_HEADER_PATTERN = re.compile(
    rb"(?ix)^\s*authorization\s*:\s*(?P<value>(?:bearer|basic)\s+[^\r\n,;]+?)\s*$"
)
_SECRET_COOKIE_HEADER_PATTERN = re.compile(
    rb"(?ix)^\s*(?P<key>cookie|set[._ -]?cookie)\s*:\s*(?P<value>[^\r\n]+?)\s*$"
)
_ENV_SECRET_REFERENCE_PATTERN = re.compile(
    rb"(?ix)^(?:"
    rb"\$\{[A-Z_][A-Z0-9_]*\}|"
    rb"%[A-Z_][A-Z0-9_]*%|"
    rb"![A-Z_][A-Z0-9_]*!|"
    rb"\$env:[A-Z_][A-Z0-9_]*|"
    rb"\$\{env:[A-Z_][A-Z0-9_]*\}|"
    rb"\$\{\{[ \t]*secrets\.[A-Z_][A-Z0-9_]*[ \t]*\}\}|"
    rb"\$[A-Z_][A-Z0-9_]*"
    rb")$"
)
_PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
    re.IGNORECASE,
)
_GITHUB_TOKEN_PATTERN = re.compile(
    rb"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})(?![A-Za-z0-9_])"
)
_AWS_ACCESS_KEY_PATTERN = re.compile(
    rb"(?<![A-Za-z0-9_])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9_])"
)
_YAML_SECRET_BLOCK_PATTERN = re.compile(
    rb"(?ix)^"
    rb"(?P<indent>[ \t]*)"
    rb"(?:-[ \t]+)?"
    rb"(?P<keyquote>[\"']?)"
    rb"(?P<key>" + _SECRET_KEY_PATTERN + rb")"
    rb"(?P=keyquote)[ \t]*:[ \t]*"
    rb"(?P<style>[>|])"
    rb"(?:(?:[1-9][+-]?)|(?:[+-][1-9]?)|[+-]?)?"
    rb"[ \t]*(?:\#.*)?$"
)
_MIN_CONCRETE_SECRET_BYTES = 24


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


def _normalized_secret_key(value: bytes) -> bytes:
    return re.sub(rb"[\s._-]+", b"", value.strip().lower())


def _normalized_secret_value(value: bytes) -> bytes:
    value = value.strip(b" \t")
    if (
        len(value) >= 2
        and value[:1] == value[-1:]
        and value[:1] in {b'"', b"'"}
    ):
        value = value[1:-1].strip(b" \t")
    return value


def _is_secret_reference(value: bytes) -> bool:
    return _ENV_SECRET_REFERENCE_PATTERN.fullmatch(value) is not None


def _is_concrete_secret_value(key: bytes, value: bytes) -> bool:
    value = _normalized_secret_value(value)
    if not value:
        return False

    normalized_key = _normalized_secret_key(key)
    if normalized_key == b"authorization":
        match = re.fullmatch(
            rb"(?i)(?:bearer|basic)[ \t]+(?P<token>.+)",
            value,
        )
        if match is not None:
            token = match.group("token").strip(b" \t")
            return bool(token) and not _is_secret_reference(token)

    if _is_secret_reference(value):
        return False
    if normalized_key in {b"cookie", b"setcookie"}:
        return True
    return len(value) >= _MIN_CONCRETE_SECRET_BYTES


def _secret_line_candidates(line: bytes) -> Iterable[bytes]:
    yield line

    for marker in (b";", b"&", b"|"):
        start = 0
        while True:
            index = line.find(marker, start)
            if index < 0:
                break
            candidate = line[index + 1 :].lstrip(b" \t")
            if candidate:
                yield candidate
            start = index + 1

    comment_boundaries = b" \t;&|"
    for marker in (b"#", b"//"):
        start = 0
        while True:
            index = line.find(marker, start)
            if index < 0:
                break
            if index == 0 or line[index - 1] in comment_boundaries:
                candidate = line[index + len(marker) :].lstrip(b" \t")
                if candidate:
                    yield candidate
            start = index + len(marker)


def _secret_text_projection(payload: bytes) -> bytes | None:
    if payload.startswith(b"\xff\xfe\x00\x00"):
        return payload[4:].decode("utf-32-le", errors="replace").encode("utf-8")
    if payload.startswith(b"\x00\x00\xfe\xff"):
        return payload[4:].decode("utf-32-be", errors="replace").encode("utf-8")
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload[3:]
    if payload.startswith(b"\xff\xfe"):
        return payload[2:].decode("utf-16-le", errors="replace").encode("utf-8")
    if payload.startswith(b"\xfe\xff"):
        return payload[2:].decode("utf-16-be", errors="replace").encode("utf-8")
    return None


def _json_contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and isinstance(item, str):
                try:
                    key_bytes = key.encode("ascii")
                except UnicodeEncodeError:
                    key_bytes = b""
                if (
                    _SECRET_KEY_FULL_PATTERN.fullmatch(key_bytes) is not None
                    and _is_concrete_secret_value(key_bytes, item.encode("utf-8"))
                ):
                    return True
            if _json_contains_secret(item):
                return True
        return False
    if isinstance(value, list):
        return any(_json_contains_secret(item) for item in value)
    return False


def _structured_json_contains_secret(payload: bytes) -> bool:
    stripped = payload.lstrip(b" \t\r\n")
    if not stripped.startswith((b"{", b"[")):
        return False
    try:
        decoded = json.loads(stripped.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return _json_contains_secret(decoded)


def _yaml_block_contains_secret(payload: bytes) -> bool:
    lines = payload.split(b"\n")
    for index, physical in enumerate(lines):
        line = physical[:-1] if physical.endswith(b"\r") else physical
        match = _YAML_SECRET_BLOCK_PATTERN.fullmatch(line)
        if match is None:
            continue

        base_indent = len(match.group("indent"))
        chunks: list[bytes] = []
        for continuation in lines[index + 1 :]:
            current = (
                continuation[:-1]
                if continuation.endswith(b"\r")
                else continuation
            )
            if not current.strip(b" \t"):
                chunks.append(b"")
                continue
            indent = len(current) - len(current.lstrip(b" \t"))
            if indent <= base_indent:
                break
            chunks.append(current.lstrip(b" \t"))

        if not chunks:
            continue
        separator = b" " if match.group("style") == b">" else b"\n"
        value = separator.join(chunks).strip(b" \t\r\n")
        if _is_concrete_secret_value(match.group("key"), value):
            return True
    return False


def _projection_contains_secret(payload: bytes) -> bool:
    for pattern in (
        _PRIVATE_KEY_PATTERN,
        _GITHUB_TOKEN_PATTERN,
        _AWS_ACCESS_KEY_PATTERN,
    ):
        if pattern.search(payload):
            return True

    if _structured_json_contains_secret(payload) or _yaml_block_contains_secret(payload):
        return True

    for physical in payload.split(b"\n"):
        line = physical[:-1] if physical.endswith(b"\r") else physical
        for candidate in _secret_line_candidates(line):
            auth = _SECRET_AUTH_HEADER_PATTERN.fullmatch(candidate)
            if (
                auth is not None
                and _is_concrete_secret_value(
                    b"authorization",
                    auth.group("value"),
                )
            ):
                return True

            cookie = _SECRET_COOKIE_HEADER_PATTERN.fullmatch(candidate)
            if (
                cookie is not None
                and _is_concrete_secret_value(
                    cookie.group("key"),
                    cookie.group("value"),
                )
            ):
                return True

            for match in _SECRET_ASSIGNMENT_PATTERN.finditer(candidate):
                if _is_concrete_secret_value(
                    match.group("key"),
                    match.group("value"),
                ):
                    return True
    return False


def _require_no_packaged_secret_content(relative: str, payload: bytes) -> None:
    """Reject credential-shaped payload content at the canonical release boundary."""

    if relative == "Autosport.exe":
        return

    if _projection_contains_secret(payload):
        raise ValueError(
            f"release package contains secret or credential content: {relative}"
        )

    projection = _secret_text_projection(payload)
    if projection is not None and _projection_contains_secret(projection):
        raise ValueError(
            f"release package contains secret or credential content: {relative}"
        )

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
) -> tuple[Path, str]:
    _require_git_commit_sha(source_sha, field="source_sha")
    exe_path = Path(exe_path)
    start_file = Path(start_file)
    example_dir = Path(example_dir)
    diagnostic_path = Path(diagnostic_path)
    accessibility_path = Path(accessibility_path)
    keyboard_path = Path(keyboard_path)
    restart_recovery_path = Path(restart_recovery_path)
    output_zip = Path(output_zip)
    package_dir = output_zip.parent / "Autosport-V1"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)
    shutil.copy2(exe_path, package_dir / "Autosport.exe")
    shutil.copy2(start_file, package_dir / "WINDOWS_START_HERE.txt")
    shutil.copy2(diagnostic_path, package_dir / "packaged-diagnostic.json")
    shutil.copy2(accessibility_path, package_dir / "accessibility-audit.json")
    shutil.copy2(keyboard_path, package_dir / "keyboard-audit.json")
    shutil.copy2(restart_recovery_path, package_dir / "restart-recovery-audit.json")
    shutil.copytree(example_dir, package_dir / "examples" / example_dir.name)

    build_info = {
        "product": "Autosport",
        "version": "0.1.0-v1-prehuman",
        "source_sha": source_sha,
        "autosport_exe_sha256": sha256_file(package_dir / "Autosport.exe"),
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "v1_ready": False,
    }
    _write_json(package_dir / "BUILD_INFO.json", build_info)

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

    archive_members = {
        (Path("Autosport-V1") / path.relative_to(package_dir)).as_posix(): path.read_bytes()
        for path in _sorted_package_files(package_dir)
    }
    writer_sha = _write_canonical_zip(output_zip, archive_members)
    return output_zip, writer_sha


def verify_windows_package(
    package_zip: str | Path,
    *,
    expected_source_sha: str,
) -> dict[str, Any]:
    """Fail closed on release ZIP identity, canonical metadata, truth labels, or payload drift."""

    _require_git_commit_sha(expected_source_sha, field="expected_source_sha")
    package_zip = Path(package_zip)
    digest = hashlib.sha256()
    with package_zip.open("rb") as source, tempfile.SpooledTemporaryFile(
        max_size=_PACKAGE_SNAPSHOT_MEMORY_LIMIT,
        mode="w+b",
    ) as snapshot:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            snapshot.write(chunk)
        package_sha = digest.hexdigest()
        snapshot.seek(0)
        with zipfile.ZipFile(snapshot, "r") as archive:
            infos = archive.infolist()
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
            _require_canonical_zip_metadata(infos, archive_comment)
            _require_canonical_zip_local_headers(snapshot, infos)
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
                payload = archive.read(name)
                _require_no_packaged_secret_content(relative, payload)
                members[relative] = payload

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
    if build_info.get("source_sha") != expected_source_sha:
        raise ValueError("BUILD_INFO source_sha does not match exact candidate head")
    _require_false_truth_labels(build_info, "BUILD_INFO.json")
    if build_info.get("v1_ready") is not False:
        raise ValueError("BUILD_INFO.json must record v1_ready=false")
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
    return {
        "status": "PASS",
        "source_sha": expected_source_sha,
        "package_sha256": package_sha,
        "autosport_exe_sha256": exe_sha,
        "file_count": len(members),
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "v1_ready": False,
    }


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
