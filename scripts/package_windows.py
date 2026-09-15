from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from autosport.data_tool_package import bind_portable_data_tool, verify_portable_data_tool
from autosport.release_package import (
    _require_git_commit_sha,
    build_windows_package,
    verify_windows_package,
)


_SHA256_HEX = frozenset("0123456789abcdef")
_COPY_CHUNK_SIZE = 1024 * 1024


def _git_output(repo_root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"unable to prove source identity with git {' '.join(args)}") from exc
    value = completed.stdout.strip()
    if not value:
        raise ValueError(f"git {' '.join(args)} returned an empty identity")
    return value


def _fetch_source_commit(repo_root: Path, source_sha: str) -> None:
    try:
        subprocess.run(
            ["git", "fetch", "--no-tags", "--depth=1", "origin", source_sha],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "source_sha is not fetchable from the authoritative origin repository"
        ) from exc


def _github_authoritative_source_sha() -> str | None:
    event_path_text = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path_text:
        return None
    event_path = Path(event_path_text)
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("GITHUB_EVENT_PATH is not readable authoritative JSON") from exc
    if not isinstance(event, dict):
        raise ValueError("GitHub event payload must be a JSON object")

    github_repository = os.environ.get("GITHUB_REPOSITORY")
    event_repository = event.get("repository")
    event_repository_name = (
        event_repository.get("full_name")
        if isinstance(event_repository, dict)
        else None
    )
    if (
        not github_repository
        or not isinstance(event_repository_name, str)
        or event_repository_name != github_repository
    ):
        raise ValueError("GitHub event repository identity does not match GITHUB_REPOSITORY")

    pull_request = event.get("pull_request")
    if isinstance(pull_request, dict):
        head = pull_request.get("head")
        if not isinstance(head, dict):
            raise ValueError("GitHub pull request event is missing head identity")
        head_repo = head.get("repo")
        head_repo_name = (
            head_repo.get("full_name")
            if isinstance(head_repo, dict)
            else None
        )
        if head_repo_name != github_repository:
            raise ValueError(
                "official package source must be a commit from the authoritative repository"
            )
        source_sha = head.get("sha")
    else:
        source_sha = os.environ.get("GITHUB_SHA")

    _require_git_commit_sha(source_sha, field="authoritative_source_sha")
    return source_sha


def _bind_source_sha_to_checkout(source_sha: str, *, repo_root: Path) -> None:
    """Bind package source identity to authoritative CI/local Git evidence.

    The offline ZIP verifier intentionally proves only syntax, equality, truth labels,
    and payload hashes. Repository membership is a build-time concern: on GitHub
    Actions we require the source SHA to equal the same-repository event head, fetch
    that exact commit from origin, and prove its tree matches the checked-out tree.
    Outside Actions, the caller must package the exact local HEAD.
    """

    _require_git_commit_sha(source_sha, field="source_sha")
    authoritative_sha = _github_authoritative_source_sha()
    if authoritative_sha is None:
        checkout_sha = _git_output(repo_root, "rev-parse", "--verify", "HEAD")
        _require_git_commit_sha(checkout_sha, field="checkout_head_sha")
        if source_sha != checkout_sha:
            raise ValueError("source_sha does not match the exact local checkout HEAD")
        source_tree = _git_output(repo_root, "rev-parse", "--verify", "HEAD^{tree}")
    else:
        if source_sha != authoritative_sha:
            raise ValueError(
                "source_sha does not match the authoritative GitHub candidate head"
            )
        _fetch_source_commit(repo_root, source_sha)
        source_tree = _git_output(
            repo_root,
            "rev-parse",
            "--verify",
            f"{source_sha}^{{tree}}",
        )

    checkout_tree = _git_output(repo_root, "rev-parse", "--verify", "HEAD^{tree}")
    if source_tree != checkout_tree:
        raise ValueError(
            "checked-out source tree does not match the authoritative source_sha tree"
        )


def _require_sha256(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in _SHA256_HEX for character in normalized):
        raise ValueError(f"{field} must be exactly 64 lowercase hexadecimal characters")
    return normalized


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _capture_verified_file(
    source: Path,
    expected_sha256: str,
    *,
    snapshot_dir: Path,
    snapshot_name: str,
    handoff_kind: str,
) -> Path:
    """Capture one hash-bound regular file into a process-private package snapshot."""

    expected = _require_sha256(expected_sha256, field=f"{snapshot_name}_sha256")
    source = source.absolute()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    destination = snapshot_dir / snapshot_name
    temporary_path: Path | None = None

    try:
        before_path = source.lstat()
    except OSError as exc:
        raise ValueError(f"{snapshot_name} {handoff_kind} handoff is not readable: {source}") from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise ValueError(
            f"{snapshot_name} {handoff_kind} handoff must be a regular non-symlink file"
        )

    digest = hashlib.sha256()
    try:
        with source.open("rb") as source_handle:
            opened = os.fstat(source_handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(
                    f"{snapshot_name} opened {handoff_kind} handoff is not a regular file"
                )
            if _file_identity(opened) != _file_identity(before_path):
                raise ValueError(f"{snapshot_name} {handoff_kind} handoff changed before capture")

            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{snapshot_name}.",
                suffix=".tmp",
                dir=snapshot_dir,
                delete=False,
            ) as destination_handle:
                temporary_path = Path(destination_handle.name)
                while True:
                    chunk = source_handle.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    destination_handle.write(chunk)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())

            after_handle = os.fstat(source_handle.fileno())
            if _file_identity(after_handle) != _file_identity(opened):
                raise ValueError(f"{snapshot_name} {handoff_kind} handoff changed during capture")

        try:
            after_path = source.lstat()
        except OSError as exc:
            raise ValueError(
                f"{snapshot_name} {handoff_kind} handoff disappeared during capture"
            ) from exc
        if stat.S_ISLNK(after_path.st_mode) or not stat.S_ISREG(after_path.st_mode):
            raise ValueError(f"{snapshot_name} {handoff_kind} handoff was replaced during capture")
        if _file_identity(after_path) != _file_identity(before_path):
            raise ValueError(f"{snapshot_name} {handoff_kind} handoff was replaced during capture")

        actual = digest.hexdigest()
        if actual != expected:
            raise ValueError(
                f"{snapshot_name} {handoff_kind} handoff SHA-256 mismatch: "
                f"expected {expected}, got {actual}"
            )

        os.replace(temporary_path, destination)
        temporary_path = None
        return destination
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _capture_verified_executable(
    source: Path,
    expected_sha256: str,
    *,
    snapshot_dir: Path,
    snapshot_name: str,
) -> Path:
    return _capture_verified_file(
        source,
        expected_sha256,
        snapshot_dir=snapshot_dir,
        snapshot_name=snapshot_name,
        handoff_kind="executable",
    )


def _capture_verified_evidence(
    source: Path,
    expected_sha256: str,
    *,
    snapshot_dir: Path,
    snapshot_name: str,
) -> Path:
    return _capture_verified_file(
        source,
        expected_sha256,
        snapshot_dir=snapshot_dir,
        snapshot_name=snapshot_name,
        handoff_kind="evidence",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--exe-sha256", required=True)
    parser.add_argument("--data-exe", type=Path, required=True)
    parser.add_argument("--data-exe-sha256", required=True)
    parser.add_argument("--start-file", type=Path, required=True)
    parser.add_argument("--example-dir", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--diagnostic-sha256", required=True)
    parser.add_argument("--accessibility-audit", type=Path, required=True)
    parser.add_argument("--accessibility-audit-sha256", required=True)
    parser.add_argument("--keyboard-audit", type=Path, required=True)
    parser.add_argument("--keyboard-audit-sha256", required=True)
    parser.add_argument("--restart-recovery-audit", type=Path, required=True)
    parser.add_argument("--restart-recovery-audit-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--verification-output", type=Path)
    args = parser.parse_args()

    _bind_source_sha_to_checkout(args.source_sha, repo_root=Path.cwd())

    with tempfile.TemporaryDirectory(prefix="autosport-package-inputs-") as snapshot_root:
        snapshot_dir = Path(snapshot_root)
        trusted_exe = _capture_verified_executable(
            args.exe,
            args.exe_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="Autosport.exe",
        )
        trusted_data_exe = _capture_verified_executable(
            args.data_exe,
            args.data_exe_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="Autosport-Data.exe",
        )
        trusted_diagnostic = _capture_verified_evidence(
            args.diagnostic,
            args.diagnostic_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="packaged-diagnostic.json",
        )
        trusted_accessibility = _capture_verified_evidence(
            args.accessibility_audit,
            args.accessibility_audit_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="accessibility-audit.json",
        )
        trusted_keyboard = _capture_verified_evidence(
            args.keyboard_audit,
            args.keyboard_audit_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="keyboard-audit.json",
        )
        trusted_restart_recovery = _capture_verified_evidence(
            args.restart_recovery_audit,
            args.restart_recovery_audit_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="restart-recovery-audit.json",
        )

        output, _base_digest = build_windows_package(
            trusted_exe,
            args.start_file,
            args.example_dir,
            trusted_diagnostic,
            trusted_accessibility,
            trusted_keyboard,
            trusted_restart_recovery,
            args.output,
            args.source_sha,
        )
        binding = bind_portable_data_tool(output, trusted_data_exe)

    verification = verify_windows_package(output, expected_source_sha=args.source_sha)
    data_verification = verify_portable_data_tool(output)
    verification.update(data_verification)
    verification["package_sha256"] = binding["package_sha256"]
    if args.verification_output is not None:
        args.verification_output.parent.mkdir(parents=True, exist_ok=True)
        args.verification_output.write_text(
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"PACKAGE={output}")
    print(f"SHA256={binding['package_sha256']}")
    print("PACKAGE_VERIFICATION=PASS")
    print("PORTABLE_DATA_TOOLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())