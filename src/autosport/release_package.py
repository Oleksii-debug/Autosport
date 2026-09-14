from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

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

    _require_git_commit_sha(expected_source_sha, field="expected_source_sha")
    package_zip = Path(package_zip)
    with zipfile.ZipFile(package_zip, "r") as archive:
        infos = archive.infolist()
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
    exe_sha = _sha256_bytes(members["Autosport.exe"])
    if build_info.get("autosport_exe_sha256") != exe_sha:
        raise ValueError("BUILD_INFO Autosport.exe hash mismatch")

    manifest = _decode_json_object(members["PACKAGE_MANIFEST.json"], "PACKAGE_MANIFEST.json")
    schema_version = manifest.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
        or not isinstance(manifest.get("files"), dict)
    ):
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
    keyboard = _decode_json_object(members["keyboard-audit.json"], "keyboard-audit.json")
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
    _require_process_recovery_evidence(restart_recovery, "restart-recovery-audit.json")

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
        if any(ord(character) < 32 or character in _WINDOWS_INVALID_CHARS for character in component):
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
        raise ValueError(f"{label} contains duplicate JSON object key: {exc.args[0]}") from exc
    except _NonStandardJsonConstantError as exc:
        raise ValueError(f"{label} contains non-standard JSON constant: {exc.args[0]}") from exc
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
