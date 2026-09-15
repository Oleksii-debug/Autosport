from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path, PurePosixPath

_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_NORMALIZED_DIST_RE = re.compile(r"[-_.]+")
_SAFE_EGG_INFO_FILES = frozenset(
    {
        "PKG-INFO",
        "SOURCES.txt",
        "dependency_links.txt",
        "entry_points.txt",
        "requires.txt",
        "top_level.txt",
    }
)


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


def _project_egg_info_contract(repo_root: Path) -> tuple[str, dict[str, str]]:
    pyproject = repo_root / "pyproject.toml"
    try:
        payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("tracked pyproject.toml is not readable canonical TOML") from exc

    project = payload.get("project")
    if not isinstance(project, dict):
        raise ValueError("tracked pyproject.toml is missing [project] metadata")
    name = project.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("tracked pyproject.toml has invalid project.name")

    scripts = project.get("scripts", {})
    if not isinstance(scripts, dict) or not all(
        isinstance(key, str)
        and key.strip()
        and isinstance(value, str)
        and value.strip()
        for key, value in scripts.items()
    ):
        raise ValueError("tracked pyproject.toml has invalid project.scripts metadata")

    normalized = _NORMALIZED_DIST_RE.sub("_", name).lower()
    return f"{normalized}.egg-info", dict(scripts)


def _validate_project_entry_points(
    repo_root: Path,
    relative_path: str,
    *,
    expected_scripts: dict[str, str],
) -> None:
    entry_points_path = repo_root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        text = entry_points_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("editable project entry_points.txt is not readable UTF-8") from exc

    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        raise ValueError("editable project entry_points.txt is not canonical INI") from exc

    if set(parser.sections()) != {"console_scripts"}:
        raise ValueError(
            "editable project entry_points.txt does not match tracked pyproject.toml"
        )
    actual_scripts = {
        name.strip(): value.strip()
        for name, value in parser.items("console_scripts", raw=True)
    }
    if actual_scripts != expected_scripts:
        raise ValueError(
            "editable project entry_points.txt does not match tracked pyproject.toml"
        )


def _is_expected_late_generated_ignored_path(repo_root: Path, path: str) -> bool:
    """Return True only for repository-local outputs that cannot redefine build source.

    Source-adjacent bytecode caches are deliberately *not* accepted: unchecked-hash .pyc files
    can execute bytes that are not represented by tracked source.  PyInstaller build/dist outputs
    and its root .spec files are generated products, while pytest cache is non-executable metadata.
    Setuptools egg-info is constrained to the one project distribution, a fixed leaf set, and an
    entry-point map that must exactly match tracked pyproject.toml; arbitrary pyinstaller40 hook
    metadata therefore fails closed.
    """

    parts = PurePosixPath(path).parts
    if not parts:
        return False
    if ".pytest_cache" in parts:
        return True
    if parts[0] in {"build", "dist"}:
        return True
    if len(parts) == 1 and parts[0].endswith(".spec"):
        return True

    egg_info_indexes = [
        index for index, part in enumerate(parts) if part.endswith(".egg-info")
    ]
    if len(egg_info_indexes) != 1:
        return False
    egg_info_index = egg_info_indexes[0]
    if egg_info_index != len(parts) - 2:
        return False

    expected_egg_info, expected_scripts = _project_egg_info_contract(repo_root)
    if parts[egg_info_index] != expected_egg_info:
        return False
    leaf = parts[-1]
    if leaf not in _SAFE_EGG_INFO_FILES:
        return False
    if leaf == "entry_points.txt":
        _validate_project_entry_points(
            repo_root,
            path,
            expected_scripts=expected_scripts,
        )
    return True


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
    """Reject source/input changes at each late release source-consuming boundary."""

    dirty = _ordinary_checkout_changes(repo_root)
    unexpected_ignored = [
        path
        for path in _ignored_checkout_paths(repo_root)
        if not _is_expected_late_generated_ignored_path(repo_root, path)
    ]
    dirty.extend(f"ignored:{path}" for path in unexpected_ignored)
    if dirty:
        raise ValueError(
            "release build source changed after initial preflight before source-consuming step: "
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
        help="re-prove exact HEAD and reject post-preflight source changes before a source-consuming release step",
    )
    args = parser.parse_args()
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
