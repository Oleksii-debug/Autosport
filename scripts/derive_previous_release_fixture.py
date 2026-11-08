"""Derive a prior-version envelope from one exact qualified Windows package.

This is test-fixture tooling for the Windows release lifecycle gate.  It does
not claim a historical release: executable bytes, source SHA and all supply-
chain evidence remain those of the exact candidate.  Only the canonical update
version is lowered so the packaged updater can execute its real transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path

from autosport import data_tool_package, release_package


_CURRENT_VERSION = "0.1.0"
_PREVIOUS_FIXTURE_VERSION = "0.0.0"
_UPDATE_SCHEMA = 1


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def derive_previous_release_fixture(
    current_archive: Path,
    output_archive: Path,
    *,
    expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, object]:
    if current_archive.resolve() == output_archive.resolve():
        raise ValueError("previous-release fixture cannot overwrite its exact input")
    payload = current_archive.read_bytes()
    if _sha256(payload) != expected_package_sha256:
        raise ValueError("current release fixture input differs from pinned digest")

    output_archive.parent.mkdir(parents=True, exist_ok=True)
    snapshot: Path | None = None
    pending: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".autosport-current-release.", suffix=".zip",
            dir=output_archive.parent, delete=False,
        ) as stream:
            snapshot = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        verified = release_package.verify_windows_package(
            snapshot,
            expected_source_sha=expected_source_sha,
            require_source_manifest=True,
            require_dependency_metadata=True,
            require_dependency_graph=True,
            require_dependency_license_evidence=True,
            require_embedded_executable_inventory=True,
            require_binary_module_attribution=True,
        )
        if verified.get("package_sha256") != expected_package_sha256:
            raise ValueError("current release verifier drifted from pinned digest")
        data_tool_package.verify_portable_data_tool(
            snapshot,
            require_data_executable_inventory=True,
            require_data_binary_module_attribution=True,
        )

        members = data_tool_package._read_members(io.BytesIO(payload))
        build_info = release_package._decode_json_object(
            members["BUILD_INFO.json"], "BUILD_INFO.json"
        )
        if (
            build_info.get("source_sha") != expected_source_sha
            or build_info.get("version") != _CURRENT_VERSION
            or build_info.get("update_version") != _CURRENT_VERSION
            or type(build_info.get("update_schema")) is not int
            or build_info.get("update_schema") != _UPDATE_SCHEMA
        ):
            raise ValueError("current release has no canonical update identity")
        build_info["version"] = _PREVIOUS_FIXTURE_VERSION
        build_info["update_version"] = _PREVIOUS_FIXTURE_VERSION
        members["BUILD_INFO.json"] = release_package._canonical_json_bytes(build_info)

        manifest_files = {
            name: _sha256(value)
            for name, value in sorted(members.items())
            if name not in {"PACKAGE_MANIFEST.json", "SHA256SUMS.txt"}
        }
        members["PACKAGE_MANIFEST.json"] = release_package._canonical_json_bytes(
            {"schema_version": 1, "files": manifest_files}
        )
        sums = {
            name: _sha256(value)
            for name, value in sorted(members.items())
            if name != "SHA256SUMS.txt"
        }
        members["SHA256SUMS.txt"] = "".join(
            f"{sums[name]}  {name}\n" for name in sorted(sums)
        ).encode("utf-8")

        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{output_archive.name}.", suffix=".tmp",
            dir=output_archive.parent, delete=False,
        ) as stream:
            pending = Path(stream.name)
        fixture_sha = data_tool_package._write_deterministic(pending, members)
        fixture_verified = release_package.verify_windows_package(
            pending, expected_source_sha=expected_source_sha,
            require_source_manifest=True,
        )
        if fixture_verified.get("package_sha256") != fixture_sha:
            raise ValueError("previous-release fixture failed canonical readback")
        data_tool_package.verify_portable_data_tool(pending)
        os.replace(pending, output_archive)
        pending = None
        return {
            "status": "PASS",
            "evidence_class": "FIXTURE_ONLY_PREVIOUS_VERSION_ENVELOPE",
            "source_sha": expected_source_sha,
            "package_sha256": fixture_sha,
            "update_version": _PREVIOUS_FIXTURE_VERSION,
            "update_schema": _UPDATE_SCHEMA,
            "historical_release": False,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
    finally:
        if snapshot is not None:
            snapshot.unlink(missing_ok=True)
        if pending is not None:
            pending.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--package-sha256", required=True)
    args = parser.parse_args(argv)
    result = derive_previous_release_fixture(
        args.current_archive,
        args.output,
        expected_source_sha=args.source_sha,
        expected_package_sha256=args.package_sha256,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
