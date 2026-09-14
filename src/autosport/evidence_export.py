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


def _is_canonical_run_summary_name(name: str) -> bool:
    if not name.startswith("run-") or not name.endswith(".json"):
        return False
    raw_id = name[len("run-") : -len(".json")]
    try:
        parsed = uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == raw_id


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
    if output_path.parent != workspace_root:
        return
    if output_path.name in _FIXED_EVIDENCE_NAMES or _is_canonical_run_summary_name(output_path.name):
        raise ValueError("output path must not overwrite canonical workspace evidence")


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

    atomic_write_json(destination, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-export-evidence",
        description="Export a deterministic metadata-only Autosport workspace evidence manifest",
    )
    parser.add_argument("workspace", type=Path, help="existing Autosport workspace")
    parser.add_argument("--output", type=Path, required=True, help="destination JSON manifest")
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


if __name__ == "__main__":
    raise SystemExit(main())
