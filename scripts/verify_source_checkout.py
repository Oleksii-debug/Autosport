from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath

_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _require_git_commit_sha(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _GIT_SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical 40-character lowercase hexadecimal Git commit SHA")
    return value


def _git_output(repo_root: Path, *args: str, allow_empty: bool = False) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
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
    event_repository_name = (
        event_repository.get("full_name") if isinstance(event_repository, dict) else None
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
        head_repo_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
        if head_repo_name != github_repository:
            raise ValueError(
                "official build source must be a commit from the authoritative repository"
            )
        source_sha = head.get("sha")
    else:
        source_sha = os.environ.get("GITHUB_SHA")

    return _require_git_commit_sha(source_sha, field="authoritative_source_sha")


def _ordinary_checkout_changes(repo_root: Path) -> list[str]:
    ordinary = _git_output(
        repo_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        allow_empty=True,
    )
    return [line for line in ordinary.splitlines() if line.strip()]


def _ignored_checkout_paths(repo_root: Path) -> list[str]:
    ignored = _git_output(
        repo_root,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        allow_empty=True,
    )
    return [line for line in ignored.splitlines() if line.strip()]


def _is_expected_late_generated_ignored_path(path: str) -> bool:
    """Allow only prior release outputs that are not live Python/package metadata inputs."""

    parts = PurePosixPath(path).parts
    if not parts:
        return False
    return (
        parts[0] == ".pytest_cache"
        or parts[0] == "build"
        or parts[0] == "dist"
        or (len(parts) == 1 and parts[0].endswith(".spec"))
    )


def _generated_build_inputs(
    repo_root: Path,
) -> tuple[list[PurePosixPath], list[PurePosixPath]]:
    paths: list[PurePosixPath] = []
    roots: set[PurePosixPath] = set()
    for path in _ignored_checkout_paths(repo_root):
        relative = PurePosixPath(path)
        for index, part in enumerate(relative.parts):
            if part == "__pycache__" or part.endswith(".egg-info"):
                paths.append(relative)
                roots.add(PurePosixPath(*relative.parts[: index + 1]))
                break
    ordered_roots = sorted(
        roots,
        key=lambda item: (len(item.parts), item.as_posix()),
        reverse=True,
    )
    return paths, ordered_roots


def clean_late_generated_build_inputs(repo_root: Path) -> None:
    """Remove ignored executable/cache metadata before a late release proof.

    Python bytecode caches and editable ``*.egg-info`` are generated during install/tests but can
    affect later Python/PyInstaller source consumption. Only exact ignored paths reported by Git
    are removed; tracked files are never selected for cleanup.
    """

    paths, roots = _generated_build_inputs(repo_root)
    for relative in paths:
        target = repo_root.joinpath(*relative.parts)
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
        except OSError as exc:
            raise ValueError(
                f"unable to remove generated build input before release proof: {relative.as_posix()}"
            ) from exc

    for relative in roots:
        target = repo_root.joinpath(*relative.parts)
        try:
            target.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # Non-empty generated roots remain visible to the subsequent fail-closed proof.
            pass


def _format_dirty_preview(dirty: list[str]) -> str:
    preview = ", ".join(dirty[:8])
    suffix = "" if len(dirty) <= 8 else f" (+{len(dirty) - 8} more)"
    return f"{preview}{suffix}"


def _require_pristine_checkout(repo_root: Path) -> None:
    dirty = _ordinary_checkout_changes(repo_root)
    dirty.extend(f"ignored:{line}" for line in _ignored_checkout_paths(repo_root))
    if dirty:
        raise ValueError(
            "release build checkout is not pristine before dependency install/PyInstaller: "
            f"{_format_dirty_preview(dirty)}"
        )


def _require_late_build_boundary_unchanged(repo_root: Path) -> None:
    """Reject source/input changes at every late source-consuming release boundary."""

    dirty = _ordinary_checkout_changes(repo_root)
    unexpected_ignored = [
        path
        for path in _ignored_checkout_paths(repo_root)
        if not _is_expected_late_generated_ignored_path(path)
    ]
    dirty.extend(f"ignored:{path}" for path in unexpected_ignored)
    if dirty:
        raise ValueError(
            "release build source changed after initial preflight before source-consuming boundary: "
            f"{_format_dirty_preview(dirty)}"
        )


def verify_source_checkout(
    source_sha: str,
    *,
    repo_root: Path,
    late_build_boundary: bool = False,
) -> None:
    """Prove the exact repository state that is about to enter the Windows build."""

    _require_git_commit_sha(source_sha, field="source_sha")
    authoritative_sha = _github_authoritative_source_sha()
    if authoritative_sha is not None and source_sha != authoritative_sha:
        raise ValueError("source_sha does not match the authoritative GitHub candidate head")

    checkout_sha = _git_output(repo_root, "rev-parse", "--verify", "HEAD")
    _require_git_commit_sha(checkout_sha, field="checkout_head_sha")
    if checkout_sha != source_sha:
        raise ValueError("checked-out HEAD does not match the exact build source_sha")

    if late_build_boundary:
        _require_late_build_boundary_unchanged(repo_root)
    else:
        _require_pristine_checkout(repo_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument(
        "--late-build-boundary",
        action="store_true",
        help="re-prove exact HEAD and reject post-preflight source/input changes",
    )
    args = parser.parse_args()
    if args.late_build_boundary:
        clean_late_generated_build_inputs(Path.cwd())
    verify_source_checkout(
        args.source_sha,
        repo_root=Path.cwd(),
        late_build_boundary=args.late_build_boundary,
    )
    if args.late_build_boundary:
        print("SOURCE_CHECKOUT_LATE_BOUNDARY=PASS")
    else:
        print("SOURCE_CHECKOUT_PREFLIGHT=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
