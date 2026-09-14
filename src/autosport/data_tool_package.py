from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from autosport.release_package import (
    _FIXED_ZIP_TIME,
    _ZIP_CREATE_SYSTEM,
    _ZIP_CREATE_VERSION,
    _ZIP_EXTRACT_VERSION,
    _ZIP_INTERNAL_ATTR,
    _ZIP_RESERVED,
    _ZIP_VOLUME,
    _decode_json_object,
    _validate_windows_member,
    verify_windows_package,
)


_PREFIX = "Autosport-V1/"
_DATA_TOOL = "Autosport-Data.exe"
_BUILD_INFO = "BUILD_INFO.json"
_MANIFEST = "PACKAGE_MANIFEST.json"
_SUMS = "SHA256SUMS.txt"
_WRITER_SNAPSHOT_MEMORY_LIMIT = 8 * 1024 * 1024


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_members(package_zip: str | Path | BinaryIO) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    windows_keys: dict[str, str] = {}
    with zipfile.ZipFile(package_zip, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                raise ValueError(
                    f"release package contains unsupported directory entry: {info.filename}"
                )
            name = info.filename
            relative, windows_key = _validate_windows_member(name)
            previous = windows_keys.get(windows_key)
            if previous is not None:
                raise ValueError(
                    "release package contains Windows path collision: "
                    f"{previous} vs {name}"
                )
            windows_keys[windows_key] = name
            members[relative] = archive.read(name)
    return members


def _write_deterministic(package_zip: Path, members: dict[str, bytes]) -> str:
    """Publish one canonical ZIP and return the SHA of the exact bytes authored.

    ZIP construction happens in a private seekable stream. The digest is accumulated
    while those same completed bytes are copied into a same-directory publication
    temp file. The mutable destination path is never reread to establish writer
    identity; later semantic verification must independently match this digest.
    """

    package_zip.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.SpooledTemporaryFile(
        max_size=_WRITER_SNAPSHOT_MEMORY_LIMIT,
        mode="w+b",
    ) as authored:
        with zipfile.ZipFile(
            authored,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for relative in sorted(members):
                info = zipfile.ZipInfo((_PREFIX + relative), _FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = _ZIP_CREATE_SYSTEM
                info.create_version = _ZIP_CREATE_VERSION
                info.extract_version = _ZIP_EXTRACT_VERSION
                info.reserved = _ZIP_RESERVED
                info.flag_bits = 0
                info.volume = _ZIP_VOLUME
                info.internal_attr = _ZIP_INTERNAL_ATTR
                info.external_attr = (0o755 if relative.lower().endswith(".exe") else 0o644) << 16
                archive.writestr(
                    info,
                    members[relative],
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


def _verified_base_members(
    package: Path,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
    """Verify and return members plus evidence from one immutable byte capture."""

    base_bytes = package.read_bytes()
    captured_sha = _sha256_bytes(base_bytes)

    # Member extraction is from the immutable in-memory capture, never from the
    # temporary pathname used only to adapt the path-based package verifier.
    members = _read_members(io.BytesIO(base_bytes))
    for required in (_BUILD_INFO, _MANIFEST, _SUMS, "Autosport.exe"):
        if required not in members:
            raise ValueError(f"base release package is missing required member: {required}")
    build_info = _decode_json_object(members[_BUILD_INFO], _BUILD_INFO)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{package.name}.verify.",
        suffix=".zip",
        dir=package.parent,
    )
    snapshot = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(base_bytes)
            handle.flush()
            os.fsync(handle.fileno())

        verification = verify_windows_package(
            snapshot,
            expected_source_sha=build_info.get("source_sha"),
        )
        if verification.get("package_sha256") != captured_sha:
            raise ValueError(
                "release package verification snapshot does not match captured package bytes"
            )
        return members, build_info, verification
    finally:
        if snapshot.exists():
            snapshot.unlink()


def bind_portable_data_tool(package_zip: str | Path, data_exe: str | Path) -> dict[str, str]:
    """Add the console data tool only after the exact captured base package verifies cleanly."""

    package = Path(package_zip)
    data_path = Path(data_exe)
    data_bytes = data_path.read_bytes()
    if not data_bytes:
        raise ValueError("portable data tool executable is empty")

    members, build_info, _verification = _verified_base_members(package)

    members[_DATA_TOOL] = data_bytes
    data_sha = _sha256_bytes(data_bytes)
    build_info["autosport_data_exe_sha256"] = data_sha
    build_info["portable_historical_data_tools"] = True
    members[_BUILD_INFO] = _json_bytes(build_info)

    manifest_files = {
        relative: _sha256_bytes(payload)
        for relative, payload in sorted(members.items())
        if relative not in {_MANIFEST, _SUMS}
    }
    members[_MANIFEST] = _json_bytes({"schema_version": 1, "files": manifest_files})

    sums = [
        f"{_sha256_bytes(payload)}  {relative}"
        for relative, payload in sorted(members.items())
        if relative != _SUMS
    ]
    members[_SUMS] = ("\n".join(sums) + "\n").encode("utf-8")
    writer_sha = _write_deterministic(package, members)
    return {"autosport_data_exe_sha256": data_sha, "package_sha256": writer_sha}


def verify_portable_data_tool(package_zip: str | Path) -> dict[str, Any]:
    members, build_info, verification = _verified_base_members(Path(package_zip))
    if _DATA_TOOL not in members:
        raise ValueError("release package is missing Autosport-Data.exe")
    if build_info.get("portable_historical_data_tools") is not True:
        raise ValueError("BUILD_INFO does not bind portable historical data tools")
    actual = _sha256_bytes(members[_DATA_TOOL])
    if build_info.get("autosport_data_exe_sha256") != actual:
        raise ValueError("BUILD_INFO Autosport-Data.exe hash mismatch")
    return {
        **verification,
        "status": "PASS",
        "autosport_data_exe_sha256": actual,
        "portable_historical_data_tools": True,
    }
