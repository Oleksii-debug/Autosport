from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from autosport.release_package import (
    _decode_json_object,
    _validate_windows_member,
    verify_windows_package,
)


_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_PREFIX = "Autosport-V1/"
_DATA_TOOL = "Autosport-Data.exe"
_BUILD_INFO = "BUILD_INFO.json"
_MANIFEST = "PACKAGE_MANIFEST.json"
_SUMS = "SHA256SUMS.txt"
_SHA256_HEX = frozenset("0123456789abcdef")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_sha256(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in _SHA256_HEX for character in normalized):
        raise ValueError(f"{field} must be exactly 64 hexadecimal characters")
    return normalized


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_members(package_zip: Path) -> dict[str, bytes]:
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


def _write_deterministic(package_zip: Path, members: dict[str, bytes]) -> None:
    package_zip.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{package_zip.name}.", suffix=".tmp", dir=package_zip.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative in sorted(members):
                info = zipfile.ZipInfo((_PREFIX + relative), _FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 if relative.lower().endswith(".exe") else 0o644) << 16
                archive.writestr(
                    info,
                    members[relative],
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        os.replace(tmp, package_zip)
    finally:
        if tmp.exists():
            tmp.unlink()


def _verified_base_members(
    package: Path,
    *,
    expected_package_sha256: str | None = None,
    expected_autosport_exe_sha256: str | None = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Verify and return members from one immutable read of the base ZIP bytes."""

    base_bytes = package.read_bytes()
    if expected_package_sha256 is not None:
        expected_package = _require_sha256(
            expected_package_sha256,
            field="expected_base_package_sha256",
        )
        actual_package = _sha256_bytes(base_bytes)
        if actual_package != expected_package:
            raise ValueError(
                "base release package producer SHA-256 mismatch: "
                f"expected {expected_package}, got {actual_package}"
            )

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

        members = _read_members(snapshot)
        for required in (_BUILD_INFO, _MANIFEST, _SUMS, "Autosport.exe"):
            if required not in members:
                raise ValueError(f"base release package is missing required member: {required}")

        if expected_autosport_exe_sha256 is not None:
            expected_exe = _require_sha256(
                expected_autosport_exe_sha256,
                field="expected_autosport_exe_sha256",
            )
            actual_exe = _sha256_bytes(members["Autosport.exe"])
            if actual_exe != expected_exe:
                raise ValueError(
                    "base release package Autosport.exe producer SHA-256 mismatch: "
                    f"expected {expected_exe}, got {actual_exe}"
                )

        build_info = _decode_json_object(members[_BUILD_INFO], _BUILD_INFO)
        verify_windows_package(
            snapshot,
            expected_source_sha=build_info.get("source_sha"),
        )
        return members, build_info
    finally:
        if snapshot.exists():
            snapshot.unlink()


def bind_portable_data_tool(
    package_zip: str | Path,
    data_exe: str | Path,
    *,
    expected_base_package_sha256: str,
    expected_autosport_exe_sha256: str,
) -> dict[str, str]:
    """Add the console data tool only after the producer-bound base ZIP verifies cleanly."""

    package = Path(package_zip)
    data_path = Path(data_exe)
    data_bytes = data_path.read_bytes()
    if not data_bytes:
        raise ValueError("portable data tool executable is empty")

    members, build_info = _verified_base_members(
        package,
        expected_package_sha256=expected_base_package_sha256,
        expected_autosport_exe_sha256=expected_autosport_exe_sha256,
    )

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
    _write_deterministic(package, members)
    return {"autosport_data_exe_sha256": data_sha, "package_sha256": _sha256_bytes(package.read_bytes())}


def verify_portable_data_tool(package_zip: str | Path) -> dict[str, Any]:
    members, build_info = _verified_base_members(Path(package_zip))
    if _DATA_TOOL not in members:
        raise ValueError("release package is missing Autosport-Data.exe")
    if build_info.get("portable_historical_data_tools") is not True:
        raise ValueError("BUILD_INFO does not bind portable historical data tools")
    actual = _sha256_bytes(members[_DATA_TOOL])
    if build_info.get("autosport_data_exe_sha256") != actual:
        raise ValueError("BUILD_INFO Autosport-Data.exe hash mismatch")
    return {
        "status": "PASS",
        "autosport_data_exe_sha256": actual,
        "portable_historical_data_tools": True,
    }
