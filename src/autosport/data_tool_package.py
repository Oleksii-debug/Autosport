from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_PREFIX = "Autosport-V1/"
_DATA_TOOL = "Autosport-Data.exe"
_BUILD_INFO = "BUILD_INFO.json"
_MANIFEST = "PACKAGE_MANIFEST.json"
_SUMS = "SHA256SUMS.txt"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _read_members(package_zip: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(package_zip, "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or not name.startswith(_PREFIX):
                raise ValueError(f"release package contains unsafe member: {name}")
            relative = name[len(_PREFIX):]
            if not relative or relative in members:
                raise ValueError("release package contains empty or duplicate member names")
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


def bind_portable_data_tool(package_zip: str | Path, data_exe: str | Path) -> dict[str, str]:
    """Add the console data tool and rebuild release integrity metadata deterministically."""

    package = Path(package_zip)
    data_path = Path(data_exe)
    data_bytes = data_path.read_bytes()
    if not data_bytes:
        raise ValueError("portable data tool executable is empty")

    members = _read_members(package)
    for required in (_BUILD_INFO, _MANIFEST, _SUMS, "Autosport.exe"):
        if required not in members:
            raise ValueError(f"base release package is missing required member: {required}")
    members[_DATA_TOOL] = data_bytes

    build_info = json.loads(members[_BUILD_INFO].decode("utf-8"))
    if not isinstance(build_info, dict):
        raise ValueError("BUILD_INFO.json must contain an object")
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
    members = _read_members(Path(package_zip))
    if _DATA_TOOL not in members:
        raise ValueError("release package is missing Autosport-Data.exe")
    if _BUILD_INFO not in members:
        raise ValueError("release package is missing BUILD_INFO.json")
    build_info = json.loads(members[_BUILD_INFO].decode("utf-8"))
    if not isinstance(build_info, dict):
        raise ValueError("BUILD_INFO.json must contain an object")
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
