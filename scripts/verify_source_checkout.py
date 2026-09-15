from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _require_git_commit_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _GIT_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical 40-character lowercase hexadecimal Git commit SHA")
    return value


def _require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical 64-character lowercase hexadecimal SHA-256")
    return value


def _git_environment() -> dict[str, str]:
    env = os.environ.copy()
    # Git replacement refs can make a claimed commit resolve a different tree.
    # Release provenance must always read the repository's original objects.
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _git_output(repo_root: Path, *args: str, allow_empty: bool = False) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            env=_git_environment(),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"unable to prove build source with git {' '.join(args)}") from exc
    value = completed.stdout.strip()
    if not value and not allow_empty:
        raise ValueError(f"git {' '.join(args)} returned an empty identity")
    return value


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            env=_git_environment(),
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"unable to prove raw build source with git {' '.join(args)}") from exc
    return completed.stdout


def _github_authoritative_source_sha() -> str | None:
    event_path_text = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path_text:
        return None
    try:
        event = json.loads(Path(event_path_text).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("GITHUB_EVENT_PATH is not readable authoritative JSON") from exc
    if not isinstance(event, dict):
        raise ValueError("GitHub event payload must be a JSON object")

    github_repository = os.environ.get("GITHUB_REPOSITORY")
    event_repository = event.get("repository")
    event_repository_name = event_repository.get("full_name") if isinstance(event_repository, dict) else None
    if not github_repository or not isinstance(event_repository_name, str) or event_repository_name != github_repository:
        raise ValueError("GitHub event repository identity does not match GITHUB_REPOSITORY")

    pull_request = event.get("pull_request")
    if isinstance(pull_request, dict):
        head = pull_request.get("head")
        if not isinstance(head, dict):
            raise ValueError("GitHub pull request event is missing head identity")
        head_repo = head.get("repo")
        head_repo_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
        if head_repo_name != github_repository:
            raise ValueError("official build source must be a commit from the authoritative repository")
        source_sha = head.get("sha")
    else:
        source_sha = os.environ.get("GITHUB_SHA")

    return _require_git_commit_sha(source_sha, field="authoritative_source_sha")


def _untracked_checkout_paths(repo_root: Path) -> list[str]:
    untracked = _git_output(repo_root, "ls-files", "--others", "--exclude-standard", allow_empty=True)
    return [line for line in untracked.splitlines() if line.strip()]


def _ignored_checkout_paths(repo_root: Path) -> list[str]:
    ignored = _git_output(repo_root, "ls-files", "--others", "--ignored", "--exclude-standard", allow_empty=True)
    return [line for line in ignored.splitlines() if line.strip()]


def _require_no_replace_refs(repo_root: Path) -> None:
    refs = _git_output(
        repo_root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace",
        allow_empty=True,
    )
    replacement_refs = [line for line in refs.splitlines() if line.strip()]
    if replacement_refs:
        raise ValueError(
            "release build repository contains Git replacement refs: "
            f"{_format_dirty_preview(replacement_refs)}"
        )


def _require_unmasked_index(repo_root: Path) -> None:
    tagged = _git_output(repo_root, "ls-files", "-v", allow_empty=True)
    masked = [line for line in tagged.splitlines() if line and not line.startswith("H ")]
    if masked:
        raise ValueError(
            "release build index contains masked/noncanonical tracked paths: "
            f"{_format_dirty_preview(masked)}"
        )


def _source_tree_entries(repo_root: Path, source_sha: str) -> dict[bytes, tuple[bytes, bytes]]:
    tree = _git_bytes(repo_root, "ls-tree", "-r", "-z", "--full-tree", source_sha)
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_type, object_sha = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ValueError("unable to parse exact source tree") from exc
        if object_type != b"blob":
            path = path_bytes.decode("utf-8", errors="surrogateescape")
            raise ValueError(
                "release build exact source contains unsupported tracked type: "
                f"{path} ({object_type.decode('ascii', errors='replace')})"
            )
        entries[path_bytes] = (mode, object_sha)
    return entries


def _require_index_matches_source(repo_root: Path, source_sha: str) -> None:
    expected = _source_tree_entries(repo_root, source_sha)
    actual: dict[bytes, tuple[bytes, bytes]] = {}
    invalid: list[str] = []
    index = _git_bytes(repo_root, "ls-files", "--stage", "-z")
    for record in index.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_sha, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ValueError("unable to parse release build index") from exc
        path = path_bytes.decode("utf-8", errors="surrogateescape")
        if stage != b"0":
            invalid.append(f"{path} (stage {stage.decode('ascii', errors='replace')})")
            continue
        actual[path_bytes] = (mode, object_sha)

    for path_bytes in sorted(expected.keys() | actual.keys()):
        if expected.get(path_bytes) != actual.get(path_bytes):
            invalid.append(path_bytes.decode("utf-8", errors="surrogateescape"))
    if invalid:
        raise ValueError(
            "release build index does not match exact source_sha: "
            f"{_format_dirty_preview(invalid)}"
        )


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def _require_raw_tracked_bytes_match_source(repo_root: Path, source_sha: str) -> None:
    """Bind every tracked worktree byte to the exact commit without Git clean filters."""
    mismatches: list[str] = []
    for path_bytes, (_mode, object_sha) in _source_tree_entries(repo_root, source_sha).items():
        path = path_bytes.decode("utf-8", errors="surrogateescape")
        target = repo_root.joinpath(*PurePosixPath(path).parts)
        try:
            if target.is_symlink():
                data = os.fsencode(os.readlink(target))
            elif target.is_file():
                data = target.read_bytes()
            else:
                mismatches.append(f"{path} (missing/non-file)")
                continue
        except OSError:
            mismatches.append(f"{path} (unreadable)")
            continue

        actual_sha = _git_blob_sha1(data)
        expected_sha = object_sha.decode("ascii")
        if actual_sha != expected_sha:
            mismatches.append(path)

    if mismatches:
        raise ValueError(
            "release build raw tracked bytes do not match exact source_sha: "
            f"{_format_dirty_preview(mismatches)}"
        )


def _is_expected_late_generated_ignored_path(path: str, *, allow_release_outputs: bool) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    if parts[0] == ".pytest_cache":
        return True
    if not allow_release_outputs:
        return False
    return parts[0] in {"build", "dist"} or (len(parts) == 1 and parts[0].endswith(".spec"))


def _generated_build_inputs(repo_root: Path) -> tuple[list[PurePosixPath], list[PurePosixPath]]:
    paths: list[PurePosixPath] = []
    roots: set[PurePosixPath] = set()
    for path in _ignored_checkout_paths(repo_root):
        relative = PurePosixPath(path)
        for index, part in enumerate(relative.parts):
            if part == "__pycache__" or part.endswith(".egg-info"):
                paths.append(relative)
                roots.add(PurePosixPath(*relative.parts[: index + 1]))
                break
    ordered_roots = sorted(roots, key=lambda item: (len(item.parts), item.as_posix()), reverse=True)
    return paths, ordered_roots


def clean_late_generated_build_inputs(repo_root: Path) -> None:
    paths, roots = _generated_build_inputs(repo_root)
    for relative in paths:
        target = repo_root.joinpath(*relative.parts)
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
        except OSError as exc:
            raise ValueError(f"unable to remove generated build input before release proof: {relative.as_posix()}") from exc
    for relative in roots:
        target = repo_root.joinpath(*relative.parts)
        try:
            target.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _format_dirty_preview(dirty: list[str]) -> str:
    preview = ", ".join(dirty[:8])
    suffix = "" if len(dirty) <= 8 else f" (+{len(dirty) - 8} more)"
    return f"{preview}{suffix}"


def _require_pristine_checkout(repo_root: Path, source_sha: str) -> None:
    _require_unmasked_index(repo_root)
    # Preserve the early raw-byte failure for ordinary tracked drift, then seal
    # again after every Git metadata query. No content-filtering Git command is
    # used inside this proof.
    _require_raw_tracked_bytes_match_source(repo_root, source_sha)
    _require_index_matches_source(repo_root, source_sha)
    dirty = _untracked_checkout_paths(repo_root)
    dirty.extend(f"ignored:{line}" for line in _ignored_checkout_paths(repo_root))
    if dirty:
        raise ValueError(
            "release build checkout is not pristine before dependency install/PyInstaller: "
            f"{_format_dirty_preview(dirty)}"
        )
    _require_raw_tracked_bytes_match_source(repo_root, source_sha)


def _require_late_build_boundary_unchanged(
    repo_root: Path,
    source_sha: str,
    *,
    allow_release_outputs: bool,
) -> None:
    _require_unmasked_index(repo_root)
    _require_raw_tracked_bytes_match_source(repo_root, source_sha)
    _require_index_matches_source(repo_root, source_sha)
    dirty = _untracked_checkout_paths(repo_root)
    unexpected_ignored = [
        path for path in _ignored_checkout_paths(repo_root)
        if not _is_expected_late_generated_ignored_path(path, allow_release_outputs=allow_release_outputs)
    ]
    dirty.extend(f"ignored:{path}" for path in unexpected_ignored)
    if dirty:
        raise ValueError(
            "release build source changed after initial preflight before source-consuming boundary: "
            f"{_format_dirty_preview(dirty)}"
        )
    _require_raw_tracked_bytes_match_source(repo_root, source_sha)


def verify_source_checkout(
    source_sha: str,
    *,
    repo_root: Path,
    late_build_boundary: bool = False,
    allow_release_outputs: bool = False,
) -> None:
    _require_git_commit_sha(source_sha, field="source_sha")
    authoritative_sha = _github_authoritative_source_sha()
    if authoritative_sha is not None and source_sha != authoritative_sha:
        raise ValueError("source_sha does not match the authoritative GitHub candidate head")

    _require_no_replace_refs(repo_root)
    checkout_sha = _git_output(repo_root, "rev-parse", "--verify", "HEAD")
    _require_git_commit_sha(checkout_sha, field="checkout_head_sha")
    if checkout_sha != source_sha:
        raise ValueError("checked-out HEAD does not match the exact build source_sha")

    if late_build_boundary:
        _require_late_build_boundary_unchanged(
            repo_root,
            source_sha,
            allow_release_outputs=allow_release_outputs,
        )
    else:
        if allow_release_outputs:
            raise ValueError("allow_release_outputs requires late_build_boundary")
        _require_pristine_checkout(repo_root, source_sha)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bind_release_artifact(source: Path, destination: Path) -> str:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"release artifact source must be a regular file: {source}")
    data = source.read_bytes()
    digest = _sha256_bytes(data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return digest


def require_artifact_sha256(path: Path, expected_sha256: str) -> None:
    expected = _require_sha256(expected_sha256, field="expected_sha256")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"bound release artifact must be a regular file: {path}")
    actual = _sha256_bytes(path.read_bytes())
    if actual != expected:
        raise ValueError(f"bound release artifact SHA-256 mismatch: {path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha")
    parser.add_argument("--late-build-boundary", action="store_true")
    parser.add_argument("--allow-release-outputs", action="store_true")
    parser.add_argument("--bind-artifact", type=Path)
    parser.add_argument("--bound-output", type=Path)
    parser.add_argument("--digest-output", type=Path)
    parser.add_argument("--verify-artifact", type=Path)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()

    if args.bind_artifact is not None:
        if args.source_sha or args.verify_artifact is not None or args.bound_output is None or args.digest_output is None:
            parser.error("--bind-artifact requires --bound-output/--digest-output and no source/verify mode")
        digest = bind_release_artifact(args.bind_artifact, args.bound_output)
        args.digest_output.parent.mkdir(parents=True, exist_ok=True)
        args.digest_output.write_text(digest + "\n", encoding="utf-8")
        print("ARTIFACT_BIND=PASS")
        return 0

    if args.verify_artifact is not None:
        if args.source_sha or args.bind_artifact is not None or args.expected_sha256 is None:
            parser.error("--verify-artifact requires --expected-sha256 and no source/bind mode")
        require_artifact_sha256(args.verify_artifact, args.expected_sha256)
        print("ARTIFACT_SHA256=PASS")
        return 0

    if args.source_sha is None:
        parser.error("--source-sha is required for source checkout verification")
    if args.allow_release_outputs and not args.late_build_boundary:
        parser.error("--allow-release-outputs requires --late-build-boundary")
    if args.late_build_boundary:
        clean_late_generated_build_inputs(Path.cwd())
    verify_source_checkout(
        args.source_sha,
        repo_root=Path.cwd(),
        late_build_boundary=args.late_build_boundary,
        allow_release_outputs=args.allow_release_outputs,
    )
    print("SOURCE_CHECKOUT_LATE_BOUNDARY=PASS" if args.late_build_boundary else "SOURCE_CHECKOUT_PREFLIGHT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())