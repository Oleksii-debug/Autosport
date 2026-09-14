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
    _decode_json_object,
    _validate_windows_member,
    _write_canonical_zip,
    verify_windows_package,
)


_PREFIX = "Autosport-V1/"
_DATA_TOOL = "Autosport-Data.exe"
_BUILD_INFO = "BUILD_INFO.json"
_MANIFEST = "PACKAGE_MANIFEST.json"
_SUMS = "SHA256SUMS.txt"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_sha256_digest(value: str, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a canonical lowercase SHA-256 digest")
    return value


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
    """Publish one canonical ZIP and return the SHA of the exact bytes authored."""

    archive_members = {
        _PREFIX + relative: payload
        for relative, payload in members.items()
    }
    return _write_canonical_zip(package_zip, archive_members)


def _verified_base_members(
    package: Path,
    *,
    expected_package_sha256: str | None = None,
) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
    """Verify and return members plus evidence from one immutable byte capture."""

    if expected_package_sha256 is not None:
        _require_sha256_digest(
            expected_package_sha256,
            label="expected_base_package_sha256",
        )

    base_bytes = package.read_bytes()
    captured_sha = _sha256_bytes(base_bytes)
    if (
        expected_package_sha256 is not None
        and captured_sha != expected_package_sha256
    ):
        raise ValueError(
            "base release package digest does not match exact writer-bound package bytes"
        )

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


def bind_portable_data_tool(
    package_zip: str | Path,
    data_exe: str | Path,
    *,
    expected_base_package_sha256: str | None = None,
) -> dict[str, str]:
    """Add the data tool only after the exact expected base package verifies cleanly."""

    package = Path(package_zip)
    data_path = Path(data_exe)
    data_bytes = data_path.read_bytes()
    if not data_bytes:
        raise ValueError("portable data tool executable is empty")

    members, build_info, _verification = _verified_base_members(
        package,
        expected_package_sha256=expected_base_package_sha256,
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
