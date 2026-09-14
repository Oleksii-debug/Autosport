from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json
from .workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


_SCHEMA_VERSION = 1
_KIND = "autosport-workspace-evidence-manifest"
_CHUNK_SIZE = 1024 * 1024
_FIXED_EVIDENCE_NAMES = (
    "decisions.jsonl",
    "paper_book.json",
    "run_registry.json",
    "source_health.json",
)
_MANIFEST_KEYS = {
    "schema_version",
    "kind",
    "file_count",
    "files",
    "expected_fixed_evidence_paths",
    "missing_fixed_evidence_paths",
    "fixed_evidence_set_complete",
    "run_summary_count",
    "file_contents_included",
    "market_database_included",
    "raw_historical_or_provider_bytes_included",
    "environment_or_credential_values_included",
    "arbitrary_workspace_files_included",
    "real_money_execution",
    "manifest_sha256",
}
_FILE_KEYS = {"path", "size_bytes", "sha256"}
_FALSE_TRUTH_FIELDS = (
    "file_contents_included",
    "market_database_included",
    "raw_historical_or_provider_bytes_included",
    "environment_or_credential_values_included",
    "arbitrary_workspace_files_included",
    "real_money_execution",
)


def _is_canonical_run_summary_name(name: str) -> bool:
    if not name.startswith("run-") or not name.endswith(".json"):
        return False
    raw_id = name[len("run-") : -len(".json")]
    try:
        parsed = uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == raw_id


def _is_canonical_evidence_name(name: str) -> bool:
    return name in _FIXED_EVIDENCE_NAMES or _is_canonical_run_summary_name(name)


def _canonical_source_names(workspace: Path) -> tuple[str, ...]:
    names: set[str] = set()
    for name in _FIXED_EVIDENCE_NAMES:
        if os.path.lexists(workspace / name):
            names.add(name)
    for entry in workspace.iterdir():
        if _is_canonical_run_summary_name(entry.name):
            names.add(entry.name)
    return tuple(sorted(names))


def _stable_stat_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        os.path.samestat(left, right)
        and left.st_mode == right.st_mode
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _open_and_hash_regular_file(path: Path) -> tuple[int, str]:
    """Hash one stable regular-file snapshot without following path symlinks."""

    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"canonical evidence path is not a regular file: {path.name}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            opened = os.fstat(handle.fileno())
            if not _stable_stat_identity(before, opened):
                raise ValueError(f"canonical evidence path changed before snapshot: {path.name}")

            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)

            opened_after = os.fstat(handle.fileno())
            if not _stable_stat_identity(opened, opened_after):
                raise ValueError(f"canonical evidence file mutated during snapshot: {path.name}")
            path_after = os.stat(path, follow_symlinks=False)
            if not _stable_stat_identity(opened_after, path_after):
                raise ValueError(f"canonical evidence path changed during snapshot: {path.name}")
            return size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _manifest_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolved(path: Path, *, strict: bool) -> Path:
    try:
        return path.resolve(strict=strict)
    except OSError as exc:
        raise ValueError(f"cannot resolve path {path}: {exc}") from exc


def _reject_output_collision(workspace: Path, output: Path) -> None:
    workspace_root = _resolved(workspace, strict=True)
    output_path = _resolved(output, strict=False)
    try:
        output_path.relative_to(workspace_root)
    except ValueError:
        return
    raise ValueError(
        "output path must be outside the Autosport workspace; "
        "must not overwrite canonical workspace evidence"
    )


def _reject_duplicate_manifest_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("evidence manifest contains duplicate JSON object keys")
        result[key] = value
    return result


def _reject_manifest_constant(value: str) -> None:
    raise ValueError("evidence manifest contains non-standard JSON constants")


def _is_sha256_text(value: object) -> bool:
    if type(value) is not str or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _validate_manifest_payload(raw: object) -> dict[str, Any]:
    if type(raw) is not dict:
        raise ValueError("evidence manifest root must be a JSON object")
    if set(raw) != _MANIFEST_KEYS:
        raise ValueError("evidence manifest fields do not match schema version 1")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("evidence manifest schema_version must be exact integer 1")
    if raw["kind"] != _KIND or type(raw["kind"]) is not str:
        raise ValueError("evidence manifest kind is invalid")
    if type(raw["file_count"]) is not int or raw["file_count"] <= 0:
        raise ValueError("evidence manifest file_count must be a positive integer")
    if type(raw["run_summary_count"]) is not int or raw["run_summary_count"] < 0:
        raise ValueError("evidence manifest run_summary_count must be a non-negative integer")
    if type(raw["files"]) is not list:
        raise ValueError("evidence manifest files must be a JSON array")
    if raw["expected_fixed_evidence_paths"] != list(_FIXED_EVIDENCE_NAMES):
        raise ValueError("evidence manifest expected fixed paths are invalid")
    if type(raw["missing_fixed_evidence_paths"]) is not list:
        raise ValueError("evidence manifest missing fixed paths must be a JSON array")
    if type(raw["fixed_evidence_set_complete"]) is not bool:
        raise ValueError("evidence manifest fixed completeness flag must be boolean")
    for field in _FALSE_TRUTH_FIELDS:
        if raw[field] is not False:
            raise ValueError(f"evidence manifest truth field must be false: {field}")
    if not _is_sha256_text(raw["manifest_sha256"]):
        raise ValueError("evidence manifest manifest_sha256 is invalid")

    files = raw["files"]
    if len(files) != raw["file_count"]:
        raise ValueError("evidence manifest file_count does not match files")

    paths: list[str] = []
    for item in files:
        if type(item) is not dict or set(item) != _FILE_KEYS:
            raise ValueError("evidence manifest file record is invalid")
        path = item["path"]
        if type(path) is not str or not _is_canonical_evidence_name(path):
            raise ValueError("evidence manifest contains a noncanonical evidence path")
        if type(item["size_bytes"]) is not int or item["size_bytes"] < 0:
            raise ValueError("evidence manifest file size must be a non-negative integer")
        if not _is_sha256_text(item["sha256"]):
            raise ValueError("evidence manifest file SHA-256 is invalid")
        paths.append(path)

    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("evidence manifest file paths must be unique and sorted")

    missing_fixed = [name for name in _FIXED_EVIDENCE_NAMES if name not in paths]
    if raw["missing_fixed_evidence_paths"] != missing_fixed:
        raise ValueError("evidence manifest missing fixed paths do not match files")
    if raw["fixed_evidence_set_complete"] is not (not missing_fixed):
        raise ValueError("evidence manifest fixed completeness flag does not match files")
    run_summary_count = sum(_is_canonical_run_summary_name(name) for name in paths)
    if raw["run_summary_count"] != run_summary_count:
        raise ValueError("evidence manifest run_summary_count does not match files")

    payload_without_hash = dict(raw)
    manifest_digest = payload_without_hash.pop("manifest_sha256")
    if _manifest_sha256(payload_without_hash) != manifest_digest:
        raise ValueError("evidence manifest manifest_sha256 does not match payload")
    return raw


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_manifest_keys,
            parse_constant=_reject_manifest_constant,
        )
    except UnicodeDecodeError as exc:
        raise ValueError("evidence manifest is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("evidence manifest is not valid JSON") from exc
    except RecursionError as exc:
        raise ValueError("evidence manifest JSON nesting is too deep") from exc
    return _validate_manifest_payload(raw)


def export_evidence_manifest(workspace: str | Path, output: str | Path) -> dict[str, Any]:
    """Publish a deterministic metadata-only manifest for canonical workspace evidence.

    The export intentionally contains no workspace file contents, market database bytes,
    raw provider/history bytes, environment values, credentials, cookies, tokens or
    arbitrary workspace files. It records only canonical relative evidence names plus
    byte counts and SHA-256 digests.
    """

    root = Path(workspace)
    destination = Path(output)
    if not root.exists() or not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    _reject_output_collision(root, destination)

    # Refuse an empty/non-evidence directory before taking the economic lock so an
    # export attempt does not create lock metadata in an unrelated empty directory.
    if not _canonical_source_names(root):
        raise ValueError("workspace contains no canonical exportable evidence")

    # Keep the exclusive critical section limited to source discovery + hashing.
    # The manifest payload is immutable ordinary Python data after this block, so a
    # slow/failing destination write must not unnecessarily block replay/settlement.
    with WorkspaceEconomicLock(root):
        names = _canonical_source_names(root)
        if not names:
            raise ValueError("workspace canonical evidence disappeared before snapshot")

        files: list[dict[str, Any]] = []
        for name in names:
            size, digest = _open_and_hash_regular_file(root / name)
            files.append(
                {
                    "path": name,
                    "size_bytes": size,
                    "sha256": digest,
                }
            )

        missing_fixed = [name for name in _FIXED_EVIDENCE_NAMES if name not in names]
        run_summary_count = sum(_is_canonical_run_summary_name(name) for name in names)
        payload: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "file_count": len(files),
            "files": files,
            "expected_fixed_evidence_paths": list(_FIXED_EVIDENCE_NAMES),
            "missing_fixed_evidence_paths": missing_fixed,
            "fixed_evidence_set_complete": not missing_fixed,
            "run_summary_count": run_summary_count,
            "file_contents_included": False,
            "market_database_included": False,
            "raw_historical_or_provider_bytes_included": False,
            "environment_or_credential_values_included": False,
            "arbitrary_workspace_files_included": False,
            "real_money_execution": False,
        }
        payload["manifest_sha256"] = _manifest_sha256(payload)

    # Re-resolve immediately before publication so a destination symlink/ancestor
    # redirected into the workspace while evidence was being snapshotted fails closed.
    _reject_output_collision(root, destination)
    atomic_write_json(destination, payload)
    return payload


def verify_evidence_manifest(manifest: str | Path, workspace: str | Path) -> dict[str, Any]:
    """Fail closed unless one manifest exactly matches current canonical workspace evidence."""

    manifest_path = Path(manifest)
    root = Path(workspace)
    payload = _load_manifest(manifest_path)
    if not root.exists() or not root.is_dir():
        raise ValueError("workspace must be an existing directory")

    expected_files = {item["path"]: item for item in payload["files"]}
    expected_names = tuple(expected_files)
    with WorkspaceEconomicLock(root):
        current_names = _canonical_source_names(root)
        if current_names != expected_names:
            raise ValueError("workspace canonical evidence set does not match manifest")
        for name in current_names:
            size, digest = _open_and_hash_regular_file(root / name)
            expected = expected_files[name]
            if size != expected["size_bytes"] or digest != expected["sha256"]:
                raise ValueError(f"workspace evidence does not match manifest: {name}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-export-evidence",
        description="Export a deterministic metadata-only Autosport workspace evidence manifest",
    )
    parser.add_argument("workspace", type=Path, help="existing Autosport workspace")
    parser.add_argument("--output", type=Path, required=True, help="destination JSON manifest")
    return parser


def build_verify_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-verify-evidence",
        description="Verify an Autosport evidence manifest against current workspace evidence",
    )
    parser.add_argument("manifest", type=Path, help="evidence manifest JSON")
    parser.add_argument("--workspace", type=Path, required=True, help="existing Autosport workspace")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = export_evidence_manifest(args.workspace, args.output)
    except (OSError, ValueError, WorkspaceEconomicLockError) as exc:
        print(f"evidence_export=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"evidence_export=PASS files={report['file_count']} "
        f"run_summaries={report['run_summary_count']} "
        f"fixed_evidence_set_complete={str(report['fixed_evidence_set_complete']).lower()} "
        f"manifest_sha256={report['manifest_sha256']}"
    )
    print(
        "file_contents_included=false market_database_included=false "
        "raw_historical_or_provider_bytes_included=false "
        "environment_or_credential_values_included=false real_money_execution=false"
    )
    print(f"output={args.output}")
    return 0


def verify_main(argv: list[str] | None = None) -> int:
    args = build_verify_parser().parse_args(argv)
    try:
        report = verify_evidence_manifest(args.manifest, args.workspace)
    except (OSError, ValueError, WorkspaceEconomicLockError) as exc:
        print(f"evidence_verify=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"evidence_verify=PASS workspace_match=true files={report['file_count']} "
        f"run_summaries={report['run_summary_count']} "
        f"fixed_evidence_set_complete={str(report['fixed_evidence_set_complete']).lower()} "
        f"manifest_sha256={report['manifest_sha256']} real_money_execution=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
