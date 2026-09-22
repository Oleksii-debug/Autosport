from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

from . import release_package as canonical_release


LEGACY_ARCHIVE_NAME = "Autosport-V1-windows-x64.zip"
STAGE_NEUTRAL_ARCHIVE_NAME = "Autosport-windows-x64.zip"
LEGACY_PREFIX = "Autosport-V1/"
STAGE_NEUTRAL_PREFIX = "Autosport/"
LEGACY_BUILD_VERSION = "0.1.0-v1-prehuman"
STAGE_NEUTRAL_BUILD_VERSION = "0.1.0-prehuman"

_REQUIRED_TRUTH_LABELS = (
    "real_money_execution",
    "human_tested",
    "nvda_verified",
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _decode_json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} is not a JSON object")
    return decoded


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _relative_members(
    archive: zipfile.ZipFile,
    *,
    prefix: str,
    label: str,
) -> dict[str, bytes]:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise ValueError(f"{label} contains duplicate member names")

    members: dict[str, bytes] = {}
    for info in infos:
        name = info.filename
        if info.is_dir():
            raise ValueError(f"{label} contains a directory entry: {name}")
        if "\\" in name or not name.startswith(prefix):
            raise ValueError(f"{label} contains a member outside {prefix}: {name}")
        relative = name[len(prefix) :]
        pure = PurePosixPath(relative)
        canonical = "/".join(pure.parts)
        if (
            not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or relative != canonical
        ):
            raise ValueError(f"{label} contains an unsafe member path: {name}")
        members[relative] = archive.read(info)
    return members


def _require_false_truth_labels(build_info: dict[str, object]) -> None:
    for field in _REQUIRED_TRUTH_LABELS:
        if build_info.get(field) is not False:
            raise ValueError(f"BUILD_INFO.json must preserve {field}=false")


def _validate_manifest(
    members: dict[str, bytes],
    *,
    label: str,
) -> dict[str, object]:
    manifest_payload = members.get("PACKAGE_MANIFEST.json")
    if manifest_payload is None:
        raise ValueError(f"{label} is missing PACKAGE_MANIFEST.json")
    manifest = _decode_json_object(manifest_payload, "PACKAGE_MANIFEST.json")
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("PACKAGE_MANIFEST.json schema is invalid")

    raw_files = manifest["files"]
    assert isinstance(raw_files, dict)
    files: dict[str, str] = {}
    for key, value in raw_files.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("PACKAGE_MANIFEST.json files map is invalid")
        files[key] = value

    expected_files = set(members).difference(
        {"PACKAGE_MANIFEST.json", "SHA256SUMS.txt"}
    )
    if set(files) != expected_files:
        raise ValueError(f"{label} PACKAGE_MANIFEST.json file set drifted")
    for relative, expected_digest in files.items():
        if _sha256_bytes(members[relative]) != expected_digest:
            raise ValueError(f"{label} manifest hash mismatch: {relative}")
    return manifest


def _validate_sums(members: dict[str, bytes], *, label: str) -> None:
    payload = members.get("SHA256SUMS.txt")
    if payload is None:
        raise ValueError(f"{label} is missing SHA256SUMS.txt")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("SHA256SUMS.txt is not valid UTF-8") from exc

    sums: dict[str, str] = {}
    for line in lines:
        digest, separator, relative = line.partition("  ")
        if (
            not separator
            or len(digest) != 64
            or not relative
            or relative in sums
        ):
            raise ValueError(f"{label} SHA256SUMS.txt contains a malformed entry")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError(
                f"{label} SHA256SUMS.txt contains a non-hex digest"
            ) from exc
        sums[relative] = digest.lower()

    expected_files = set(members).difference({"SHA256SUMS.txt"})
    if set(sums) != expected_files:
        raise ValueError(f"{label} SHA256SUMS.txt file set drifted")
    for relative, expected_digest in sums.items():
        if _sha256_bytes(members[relative]) != expected_digest:
            raise ValueError(f"{label} SHA256SUMS.txt hash mismatch: {relative}")


def _load_legacy_members(package_zip: Path) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(package_zip, "r") as archive:
            return _relative_members(
                archive,
                prefix=LEGACY_PREFIX,
                label="legacy release package",
            )
    except zipfile.BadZipFile as exc:
        raise ValueError("legacy release package is not a valid ZIP") from exc


def _load_stage_neutral_members(package_zip: Path) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(package_zip, "r") as archive:
            return _relative_members(
                archive,
                prefix=STAGE_NEUTRAL_PREFIX,
                label="stage-neutral release package",
            )
    except zipfile.BadZipFile as exc:
        raise ValueError("stage-neutral release package is not a valid ZIP") from exc


def _transform_members(
    legacy_members: dict[str, bytes],
    *,
    expected_source_sha: str,
) -> dict[str, bytes]:
    _validate_manifest(legacy_members, label="legacy release package")
    _validate_sums(legacy_members, label="legacy release package")

    build_payload = legacy_members.get("BUILD_INFO.json")
    if build_payload is None:
        raise ValueError("legacy release package is missing BUILD_INFO.json")
    build_info = _decode_json_object(build_payload, "BUILD_INFO.json")
    if build_info.get("product") != "Autosport":
        raise ValueError("BUILD_INFO.json product identity mismatch")
    if build_info.get("source_sha") != expected_source_sha:
        raise ValueError("BUILD_INFO.json source_sha does not match requested source SHA")
    _require_false_truth_labels(build_info)
    if build_info.get("version") != LEGACY_BUILD_VERSION:
        raise ValueError(
            "legacy BUILD_INFO.json version is not the exact transitional version"
        )

    transformed = dict(legacy_members)
    transformed_build = dict(build_info)
    transformed_build["version"] = STAGE_NEUTRAL_BUILD_VERSION
    transformed_build["whole_product_complete"] = False
    transformed["BUILD_INFO.json"] = _canonical_json_bytes(transformed_build)

    manifest = _decode_json_object(
        transformed["PACKAGE_MANIFEST.json"],
        "PACKAGE_MANIFEST.json",
    )
    raw_manifest_files = manifest.get("files")
    if not isinstance(raw_manifest_files, dict):
        raise ValueError("PACKAGE_MANIFEST.json files map is invalid")
    manifest_files = {str(key): str(value) for key, value in raw_manifest_files.items()}
    manifest_files["BUILD_INFO.json"] = _sha256_bytes(transformed["BUILD_INFO.json"])
    transformed_manifest: dict[str, object] = {
        "schema_version": 1,
        "files": manifest_files,
    }
    transformed["PACKAGE_MANIFEST.json"] = _canonical_json_bytes(transformed_manifest)

    sums = {
        relative: _sha256_bytes(payload)
        for relative, payload in transformed.items()
        if relative != "SHA256SUMS.txt"
    }
    transformed["SHA256SUMS.txt"] = "".join(
        f"{sums[relative]}  {relative}\n" for relative in sorted(sums)
    ).encode("utf-8")

    _validate_manifest(transformed, label="stage-neutral release package")
    _validate_sums(transformed, label="stage-neutral release package")
    return transformed


def _verify_stage_neutral_output(
    package_zip: Path,
    *,
    expected_source_sha: str,
    expected_members: dict[str, bytes],
) -> dict[str, object]:
    observed = _load_stage_neutral_members(package_zip)
    if observed != expected_members:
        raise ValueError("stage-neutral release package payload drifted during write")
    _validate_manifest(observed, label="stage-neutral release package")
    _validate_sums(observed, label="stage-neutral release package")

    build_info = _decode_json_object(observed["BUILD_INFO.json"], "BUILD_INFO.json")
    if build_info.get("product") != "Autosport":
        raise ValueError("stage-neutral BUILD_INFO.json product identity mismatch")
    if build_info.get("source_sha") != expected_source_sha:
        raise ValueError("stage-neutral BUILD_INFO.json source_sha mismatch")
    if build_info.get("version") != STAGE_NEUTRAL_BUILD_VERSION:
        raise ValueError("stage-neutral BUILD_INFO.json version drifted")
    _require_false_truth_labels(build_info)
    if build_info.get("whole_product_complete") is not False:
        raise ValueError("stage-neutral BUILD_INFO.json must preserve whole_product_complete=false")

    digest = hashlib.sha256(package_zip.read_bytes()).hexdigest()
    return {
        "status": "PASS",
        "source_sha": expected_source_sha,
        "legacy_archive_name": LEGACY_ARCHIVE_NAME,
        "archive_name": STAGE_NEUTRAL_ARCHIVE_NAME,
        "package_prefix": STAGE_NEUTRAL_PREFIX,
        "build_version": STAGE_NEUTRAL_BUILD_VERSION,
        "package_sha256": digest,
        "file_count": len(observed),
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "whole_product_complete": False,
    }


def repackage_stage_neutral_windows_release(
    legacy_package_zip: str | Path,
    output_zip: str | Path,
    *,
    expected_source_sha: str,
) -> dict[str, object]:
    """Convert the canonically verified legacy Windows ZIP to stage-neutral identity.

    This is a narrow transition authority. It never upgrades product truth: the legacy
    input must first pass the existing deep release verifier for the exact source SHA.
    The transformation changes only the archive namespace and stage/version metadata,
    then recomputes the manifest and checksum evidence affected by that metadata.
    """

    legacy_package = Path(legacy_package_zip)
    output = Path(output_zip)
    if legacy_package.name != LEGACY_ARCHIVE_NAME:
        raise ValueError(
            f"legacy release archive must be named {LEGACY_ARCHIVE_NAME}"
        )
    if output.name != STAGE_NEUTRAL_ARCHIVE_NAME:
        raise ValueError(
            f"stage-neutral release archive must be named {STAGE_NEUTRAL_ARCHIVE_NAME}"
        )
    if legacy_package.resolve() == output.resolve():
        raise ValueError("stage-neutral output must not overwrite the legacy input")

    legacy_verification = canonical_release.verify_windows_package(
        legacy_package,
        expected_source_sha=expected_source_sha,
    )
    if legacy_verification.get("status") != "PASS":
        raise ValueError("legacy release verifier did not return PASS")

    legacy_members = _load_legacy_members(legacy_package)
    transformed = _transform_members(
        legacy_members,
        expected_source_sha=expected_source_sha,
    )
    archive_members = {
        f"{STAGE_NEUTRAL_PREFIX}{relative}": payload
        for relative, payload in transformed.items()
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    canonical_release._write_canonical_zip(output, archive_members)

    evidence = _verify_stage_neutral_output(
        output,
        expected_source_sha=expected_source_sha,
        expected_members=transformed,
    )
    evidence["legacy_package_sha256"] = hashlib.sha256(
        legacy_package.read_bytes()
    ).hexdigest()
    return evidence


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json_bytes(evidence))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a verified legacy Autosport Windows release ZIP to stage-neutral identity."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)

    evidence = repackage_stage_neutral_windows_release(
        args.input,
        args.output,
        expected_source_sha=args.source_sha,
    )
    if args.evidence is not None:
        _write_evidence(args.evidence, evidence)
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
