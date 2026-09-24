from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

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
            env=_exact_git_environment(),
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
            env=_exact_git_environment(),
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


def _exact_git_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.upper().startswith("GIT_"):
            env.pop(name, None)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _exact_git_bytes(repo_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            env=_exact_git_environment(),
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"unable to materialize exact package source with git {' '.join(args)}") from exc
    return completed.stdout


def _exact_checkout_tree(repo_root: Path, source_sha: str) -> dict[bytes, tuple[bytes, bytes]]:
    """Return exact regular tracked entries as raw path -> (mode, blob sha)."""

    raw = _exact_git_bytes(
        repo_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        source_sha,
    )
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_type, object_sha = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ValueError("unable to parse exact release source tree") from exc
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            display_path = path_bytes.decode("utf-8", errors="backslashreplace")
            raise ValueError(f"unsupported tracked release source entry: {display_path}")
        if len(object_sha) != 40 or any(character not in b"0123456789abcdef" for character in object_sha):
            raise ValueError("exact release source tree returned a noncanonical blob identity")
        if path_bytes in entries:
            raise ValueError("exact release source tree returned a duplicate path")
        entries[path_bytes] = (mode, object_sha)
    if not entries:
        raise ValueError("exact release source tree is empty")
    return entries


def _require_checkout_matches_exact_source(repo_root: Path, source_sha: str) -> None:
    """Prove the package-time index and raw tracked bytes equal exact source_sha.

    This deliberately does not trust ``git status``/``git diff`` or clean filters.
    It compares stage-0 index identity to the exact commit tree and hashes the raw
    filesystem bytes directly, so assume-unchanged/skip-worktree and mutable
    ``.git`` clean-filter/attribute state cannot hide a tracked source mutation.
    """

    expected = _exact_checkout_tree(repo_root, source_sha)

    index_raw = _exact_git_bytes(repo_root, "ls-files", "--stage", "-z")
    actual_index: dict[bytes, tuple[bytes, bytes]] = {}
    for record in index_raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_sha, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ValueError("unable to parse release checkout index") from exc
        if stage != b"0":
            raise ValueError("release checkout contains a non-stage-0 index entry")
        if path_bytes in actual_index:
            raise ValueError("release checkout index contains a duplicate path")
        actual_index[path_bytes] = (mode, object_sha)
    if actual_index != expected:
        raise ValueError("release checkout index does not exactly match source_sha")

    verbose_index = _exact_git_bytes(repo_root, "ls-files", "-v", "-z")
    verbose_paths: set[bytes] = set()
    for record in verbose_index.split(b"\0"):
        if not record:
            continue
        if not record.startswith(b"H "):
            raise ValueError("release checkout index contains assume-unchanged/skip-worktree masking")
        path_bytes = record[2:]
        if path_bytes in verbose_paths:
            raise ValueError("release checkout masking census contains a duplicate path")
        verbose_paths.add(path_bytes)
    if verbose_paths != set(expected):
        raise ValueError("release checkout masking census does not match source_sha paths")

    for path_bytes, (_mode, expected_blob) in expected.items():
        try:
            relative = PurePosixPath(path_bytes.decode("utf-8", errors="strict"))
        except UnicodeDecodeError as exc:
            raise ValueError("release source paths must be valid UTF-8") from exc
        path = repo_root.joinpath(*relative.parts)
        try:
            path_stat = path.lstat()
            data = path.read_bytes()
            after_stat = path.lstat()
        except OSError as exc:
            raise ValueError(f"tracked release source is unreadable: {relative.as_posix()}") from exc
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise ValueError(f"tracked release source is not a regular file: {relative.as_posix()}")
        if _file_identity(path_stat) != _file_identity(after_stat):
            raise ValueError(f"tracked release source changed while hashing: {relative.as_posix()}")
        blob_header = f"blob {len(data)}\0".encode("ascii")
        actual_blob = hashlib.sha1(
            blob_header + data,
            usedforsecurity=False,
        ).hexdigest().encode("ascii")
        if actual_blob != expected_blob:
            raise ValueError(
                f"raw tracked release source differs from source_sha: {relative.as_posix()}"
            )


def _repo_relative_path(repo_root: Path, requested: Path, *, field: str) -> PurePosixPath:
    root = Path(os.path.abspath(repo_root))
    candidate = requested if requested.is_absolute() else root / requested
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field} must be inside the release source checkout") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{field} must name a concrete tracked source path")
    return PurePosixPath(*relative.parts)


def _exact_tree_entries(
    repo_root: Path,
    source_sha: str,
    pathspec: PurePosixPath,
) -> tuple[tuple[PurePosixPath, str], ...]:
    _require_git_commit_sha(source_sha, field="source_sha")
    raw = _exact_git_bytes(
        repo_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        source_sha,
        "--",
        pathspec.as_posix(),
    )
    entries: list[tuple[PurePosixPath, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_type, object_sha = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ValueError("unable to parse exact package source tree") from exc
        path = PurePosixPath(path_bytes.decode("utf-8", errors="strict"))
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError(
                f"unsupported tracked package payload entry: {path.as_posix()}"
            )
        object_sha_text = object_sha.decode("ascii")
        if len(object_sha_text) != 40 or any(character not in "0123456789abcdef" for character in object_sha_text):
            raise ValueError("exact package source tree returned a noncanonical blob identity")
        entries.append((path, object_sha_text))
    return tuple(entries)


def _write_exact_git_blob(
    repo_root: Path,
    object_sha: str,
    destination: Path,
) -> None:
    data = _exact_git_bytes(repo_root, "cat-file", "blob", object_sha)
    blob_header = f"blob {len(data)}\0".encode("ascii")
    actual_object_sha = hashlib.sha1(
        blob_header + data,
        usedforsecurity=False,
    ).hexdigest()
    if actual_object_sha != object_sha:
        raise ValueError(
            "exact package source Git blob identity mismatch: "
            f"expected {object_sha}, got {actual_object_sha}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _materialize_exact_static_payload(
    *,
    repo_root: Path,
    source_sha: str,
    start_file: Path,
    example_dir: Path,
    snapshot_dir: Path,
) -> tuple[Path, Path]:
    """Materialize tracked static package inputs from the exact source Git tree.

    The caller-provided paths select repository paths only. Their live working-tree
    bytes and live directory membership are intentionally never consumed here.
    """

    start_relative = _repo_relative_path(repo_root, start_file, field="start_file")
    example_relative = _repo_relative_path(repo_root, example_dir, field="example_dir")

    start_entries = _exact_tree_entries(repo_root, source_sha, start_relative)
    if len(start_entries) != 1 or start_entries[0][0] != start_relative:
        raise ValueError("start_file is not exactly one tracked regular file in source_sha")

    example_entries = _exact_tree_entries(repo_root, source_sha, example_relative)
    prefix = example_relative.as_posix().rstrip("/") + "/"
    if not example_entries:
        raise ValueError("example_dir has no tracked regular files in source_sha")
    if any(not path.as_posix().startswith(prefix) for path, _object_sha in example_entries):
        raise ValueError("example_dir exact source tree escaped its requested prefix")

    static_root = snapshot_dir / "exact-source-static"
    trusted_start = static_root.joinpath(*start_relative.parts)
    trusted_example_dir = static_root.joinpath(*example_relative.parts)
    _write_exact_git_blob(repo_root, start_entries[0][1], trusted_start)
    for path, object_sha in example_entries:
        destination = static_root.joinpath(*path.parts)
        _write_exact_git_blob(repo_root, object_sha, destination)

    materialized = tuple(
        PurePosixPath(path.relative_to(static_root).as_posix())
        for path in trusted_example_dir.rglob("*")
        if path.is_file()
    )
    expected = tuple(path for path, _object_sha in example_entries)
    if tuple(sorted(materialized, key=lambda item: item.as_posix())) != tuple(
        sorted(expected, key=lambda item: item.as_posix())
    ):
        raise ValueError("exact example payload materialization membership mismatch")
    return trusted_start, trusted_example_dir


def _require_sha256(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in _SHA256_HEX for character in normalized):
        raise ValueError(f"{field} must be exactly 64 lowercase hexadecimal characters")
    return normalized


def _require_verified_package_digest(
    binding: dict[str, object],
    verification: dict[str, object],
) -> str:
    """Fail closed unless a writer binding and later verifier identify the same ZIP bytes."""

    bound_digest = binding.get("package_sha256")
    verified_digest = verification.get("package_sha256")
    if (
        not isinstance(bound_digest, str)
        or not isinstance(verified_digest, str)
        or bound_digest != verified_digest
    ):
        raise ValueError(
            "bound package digest does not match the exact verified package snapshot"
        )
    return verified_digest


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

    repo_root = Path.cwd()
    _bind_source_sha_to_checkout(args.source_sha, repo_root=repo_root)
    _require_checkout_matches_exact_source(repo_root, args.source_sha)

    with tempfile.TemporaryDirectory(prefix="autosport-package-inputs-") as snapshot_root:
        snapshot_dir = Path(snapshot_root)
        trusted_start_file, trusted_example_dir = _materialize_exact_static_payload(
            repo_root=repo_root,
            source_sha=args.source_sha,
            start_file=args.start_file,
            example_dir=args.example_dir,
            snapshot_dir=snapshot_dir,
        )
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

        output, base_digest = build_windows_package(
            trusted_exe,
            trusted_start_file,
            trusted_example_dir,
            trusted_diagnostic,
            trusted_accessibility,
            trusted_keyboard,
            trusted_restart_recovery,
            args.output,
            args.source_sha,
        )
        binding = bind_portable_data_tool(
            output,
            trusted_data_exe,
            expected_base_package_sha256=base_digest,
        )

    release_verification = verify_windows_package(
        output,
        expected_source_sha=args.source_sha,
    )
    package_sha = _require_verified_package_digest(binding, release_verification)
    data_verification = verify_portable_data_tool(output)
    _require_verified_package_digest(binding, data_verification)
    verification = dict(release_verification)
    verification.update(data_verification)
    verification["package_sha256"] = package_sha
    if args.verification_output is not None:
        args.verification_output.parent.mkdir(parents=True, exist_ok=True)
        args.verification_output.write_text(
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"PACKAGE={output}")
    print(f"SHA256={package_sha}")
    print("PACKAGE_VERIFICATION=PASS")
    print("PORTABLE_DATA_TOOLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())