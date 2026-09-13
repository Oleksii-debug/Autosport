from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_PACKAGE_PREFIX = "Autosport-V1/"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def build_windows_package(
    exe_path: str | Path,
    start_file: str | Path,
    example_dir: str | Path,
    diagnostic_path: str | Path,
    accessibility_path: str | Path,
    output_zip: str | Path,
    source_sha: str,
) -> tuple[Path, str]:
    exe_path = Path(exe_path)
    start_file = Path(start_file)
    example_dir = Path(example_dir)
    diagnostic_path = Path(diagnostic_path)
    accessibility_path = Path(accessibility_path)
    output_zip = Path(output_zip)
    package_dir = output_zip.parent / "Autosport-V1"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)
    shutil.copy2(exe_path, package_dir / "Autosport.exe")
    shutil.copy2(start_file, package_dir / "WINDOWS_START_HERE.txt")
    shutil.copy2(diagnostic_path, package_dir / "packaged-diagnostic.json")
    shutil.copy2(accessibility_path, package_dir / "accessibility-audit.json")
    shutil.copytree(example_dir, package_dir / "examples" / example_dir.name)

    build_info = {
        "product": "Autosport",
        "version": "0.1.0-v1-prehuman",
        "source_sha": source_sha,
        "autosport_exe_sha256": sha256_file(package_dir / "Autosport.exe"),
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    _write_json(package_dir / "BUILD_INFO.json", build_info)

    payload_hashes = {}
    for path in sorted(item for item in package_dir.rglob("*") if item.is_file()):
        relative = path.relative_to(package_dir).as_posix()
        if relative == "SHA256SUMS.txt":
            continue
        payload_hashes[relative] = sha256_file(path)
    _write_json(package_dir / "PACKAGE_MANIFEST.json", {"schema_version": 1, "files": payload_hashes})

    lines = []
    for path in sorted(item for item in package_dir.rglob("*") if item.is_file() and item.name != "SHA256SUMS.txt"):
        relative = path.relative_to(package_dir).as_posix()
        lines.append(f"{sha256_file(path)}  {relative}")
    (package_dir / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if output_zip.exists():
        output_zip.unlink()
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in package_dir.rglob("*") if item.is_file()):
            relative = Path("Autosport-V1") / path.relative_to(package_dir)
            info = zipfile.ZipInfo(relative.as_posix(), _FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if path.name.lower().endswith(".exe") else 0o644) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output_zip, sha256_file(output_zip)


def verify_windows_package(
    package_zip: str | Path,
    *,
    expected_source_sha: str,
) -> dict[str, Any]:
    """Fail closed on a release ZIP whose identity, truth labels, or payload hashes drift."""

    package_zip = Path(package_zip)
    with zipfile.ZipFile(package_zip, "r") as archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise ValueError("release package contains duplicate member names")
        members: dict[str, bytes] = {}
        for name in names:
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts or not name.startswith(_PACKAGE_PREFIX):
                raise ValueError(f"release package contains unsafe member: {name}")
            relative = name[len(_PACKAGE_PREFIX):]
            if not relative:
                raise ValueError("release package contains an empty member name")
            members[relative] = archive.read(name)

    required = {
        "Autosport.exe",
        "WINDOWS_START_HERE.txt",
        "packaged-diagnostic.json",
        "accessibility-audit.json",
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
    exe_sha = _sha256_bytes(members["Autosport.exe"])
    if build_info.get("autosport_exe_sha256") != exe_sha:
        raise ValueError("BUILD_INFO Autosport.exe hash mismatch")

    manifest = _decode_json_object(members["PACKAGE_MANIFEST.json"], "PACKAGE_MANIFEST.json")
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("PACKAGE_MANIFEST schema is invalid")
    manifest_files = {str(key): str(value) for key, value in manifest["files"].items()}
    expected_manifest_files = set(members).difference({"PACKAGE_MANIFEST.json", "SHA256SUMS.txt"})
    if set(manifest_files) != expected_manifest_files:
        raise ValueError("PACKAGE_MANIFEST file set does not match package payload")
    for relative, expected_hash in manifest_files.items():
        if _sha256_bytes(members[relative]) != expected_hash:
            raise ValueError(f"PACKAGE_MANIFEST hash mismatch: {relative}")

    sums: dict[str, str] = {}
    for line in members["SHA256SUMS.txt"].decode("utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or len(digest) != 64 or not relative or relative in sums:
            raise ValueError("SHA256SUMS contains a malformed or duplicate entry")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError("SHA256SUMS contains a non-hex digest") from exc
        sums[relative] = digest.lower()
    expected_sum_files = set(members).difference({"SHA256SUMS.txt"})
    if set(sums) != expected_sum_files:
        raise ValueError("SHA256SUMS file set does not match package payload")
    for relative, expected_hash in sums.items():
        if _sha256_bytes(members[relative]) != expected_hash:
            raise ValueError(f"SHA256SUMS hash mismatch: {relative}")

    diagnostic = _decode_json_object(members["packaged-diagnostic.json"], "packaged-diagnostic.json")
    accessibility = _decode_json_object(members["accessibility-audit.json"], "accessibility-audit.json")
    for label, payload in (
        ("packaged-diagnostic.json", diagnostic),
        ("accessibility-audit.json", accessibility),
    ):
        if payload.get("status") != "PASS":
            raise ValueError(f"{label} does not record PASS")
        _require_false_truth_labels(payload, label)

    return {
        "status": "PASS",
        "source_sha": expected_source_sha,
        "package_sha256": sha256_file(package_zip),
        "autosport_exe_sha256": exe_sha,
        "file_count": len(members),
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }


def _decode_json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
