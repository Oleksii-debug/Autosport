from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from autosport.release_sbom import validate_embedded_executable_inventory
from autosport.release_binary_attribution import validate_binary_module_attribution
from autosport.release_package import (
    _decode_json_object,
    _require_release_zip_resource_bounds,
    _validate_windows_member,
    _write_canonical_zip,
    _ZIP_MAX_WIRE_BYTES,
    _ZIP_MAX_MEMBER_UNCOMPRESSED,
    verify_windows_package,
)


_PREFIX = "Autosport-V1/"
_DATA_TOOL = "Autosport-Data.exe"
_DATA_INVENTORY = "DATA_EXECUTABLE_INVENTORY.json"
_DATA_ATTRIBUTION = "DATA_BINARY_MODULE_ATTRIBUTION.json"
_DEPENDENCY_GRAPH = "DEPENDENCY_GRAPH_METADATA.json"
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
        # Bound every central-directory entry before reading any ZIP member.
        # The canonical release verifier otherwise runs only after extraction.
        infos = archive.infolist()
        _require_release_zip_resource_bounds(infos)
        for info in infos:
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

    # Bound the whole input before snapshotting it into memory.  The second
    # check catches growth between the path-size check and descriptor read.
    if package.stat().st_size > _ZIP_MAX_WIRE_BYTES:
        raise ValueError("release ZIP resource limit: excessive wire size")
    with package.open("rb") as source:
        base_bytes = source.read(_ZIP_MAX_WIRE_BYTES + 1)
    if len(base_bytes) > _ZIP_MAX_WIRE_BYTES:
        raise ValueError("release ZIP resource limit: excessive wire size")
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
    primary_error: BaseException | None = None
    try:
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException as exc:
            try:
                os.close(fd)
            except OSError as cleanup_error:
                try:
                    BaseException.add_note(
                        exc,
                        "data-tool verification descriptor cleanup also failed: "
                        f"{cleanup_error}",
                    )
                except BaseException:
                    pass
            raise
        with handle:
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
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            snapshot.unlink()
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            if primary_error is None:
                raise
            try:
                BaseException.add_note(
                    primary_error,
                    "data-tool verification snapshot cleanup also failed: "
                    f"{cleanup_error}",
                )
            except BaseException:
                pass


def bind_portable_data_tool(
    package_zip: str | Path,
    data_exe: str | Path,
    *,
    expected_base_package_sha256: str | None = None,
    data_executable_inventory: dict[str, Any] | None = None,
    data_binary_module_attribution: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Add the data tool only after the exact expected base package verifies cleanly."""

    package = Path(package_zip)
    data_path = Path(data_exe)
    # Autosport-Data.exe becomes a single release ZIP member. Enforce the
    # canonical per-member limit *before* capturing its bytes, including when
    # the file grows after the first stat; never decompress or publish it here.
    if data_path.stat().st_size > _ZIP_MAX_MEMBER_UNCOMPRESSED:
        raise ValueError("release ZIP resource limit: unsafe member size")
    with data_path.open("rb") as source:
        data_bytes = source.read(_ZIP_MAX_MEMBER_UNCOMPRESSED + 1)
    if len(data_bytes) > _ZIP_MAX_MEMBER_UNCOMPRESSED:
        raise ValueError("release ZIP resource limit: unsafe member size")
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
    if data_executable_inventory is not None:
        # A second PE is shipped: provenance of the main executable alone is
        # insufficient to describe the exact final release archive.
        validate_embedded_executable_inventory(
            data_executable_inventory,
            source_sha=build_info["source_sha"],
            executable_sha256=data_sha,
        )
        members[_DATA_INVENTORY] = _json_bytes(data_executable_inventory)
    if data_binary_module_attribution is not None:
        if data_executable_inventory is None or _DEPENDENCY_GRAPH not in members:
            raise ValueError("Data binary attribution requires sealed inventory and dependency graph")
        dependency_graph = _decode_json_object(
            members[_DEPENDENCY_GRAPH], _DEPENDENCY_GRAPH,
        )
        validate_binary_module_attribution(
            data_binary_module_attribution, dependency_graph, data_executable_inventory,
        )
        members[_DATA_ATTRIBUTION] = _json_bytes(data_binary_module_attribution)
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


def verify_portable_data_tool(
    package_zip: str | Path, *, require_data_executable_inventory: bool = False,
    require_data_binary_module_attribution: bool = False,
) -> dict[str, Any]:
    members, build_info, verification = _verified_base_members(Path(package_zip))
    if _DATA_TOOL not in members:
        raise ValueError("release package is missing Autosport-Data.exe")
    if build_info.get("portable_historical_data_tools") is not True:
        raise ValueError("BUILD_INFO does not bind portable historical data tools")
    actual = _sha256_bytes(members[_DATA_TOOL])
    if build_info.get("autosport_data_exe_sha256") != actual:
        raise ValueError("BUILD_INFO Autosport-Data.exe hash mismatch")
    if require_data_executable_inventory and _DATA_INVENTORY not in members:
        raise ValueError("release package is missing Data executable inventory")
    if _DATA_INVENTORY in members:
        inventory = _decode_json_object(
            members[_DATA_INVENTORY], _DATA_INVENTORY,
        )
        validate_embedded_executable_inventory(
            inventory, source_sha=build_info["source_sha"], executable_sha256=actual,
        )
        if members[_DATA_INVENTORY] != _json_bytes(inventory):
            raise ValueError("Data executable inventory is not canonical JSON")
    if require_data_binary_module_attribution and _DATA_ATTRIBUTION not in members:
        raise ValueError("release package is missing Data binary module attribution")
    if _DATA_ATTRIBUTION in members:
        if _DATA_INVENTORY not in members or _DEPENDENCY_GRAPH not in members:
            raise ValueError("Data binary attribution lacks sealed inventory or dependency graph")
        attribution = _decode_json_object(members[_DATA_ATTRIBUTION], _DATA_ATTRIBUTION)
        dependency_graph = _decode_json_object(
            members[_DEPENDENCY_GRAPH], _DEPENDENCY_GRAPH,
        )
        validate_binary_module_attribution(attribution, dependency_graph, inventory)
        if members[_DATA_ATTRIBUTION] != _json_bytes(attribution):
            raise ValueError("Data binary attribution is not canonical JSON")
    return {
        **verification,
        "status": "PASS",
        "autosport_data_exe_sha256": actual,
        "portable_historical_data_tools": True,
    }
