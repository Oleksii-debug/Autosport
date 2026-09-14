from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from autosport.data_tool_package import bind_portable_data_tool, verify_portable_data_tool
from autosport.release_package import _require_git_commit_sha, build_windows_package


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


def _require_verified_package_digest(
    binding: dict[str, Any],
    verification: dict[str, Any],
) -> str:
    """Fail closed unless bind and one-snapshot verification identify the same ZIP bytes."""

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--data-exe", type=Path, required=True)
    parser.add_argument("--start-file", type=Path, required=True)
    parser.add_argument("--example-dir", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--accessibility-audit", type=Path, required=True)
    parser.add_argument("--keyboard-audit", type=Path, required=True)
    parser.add_argument("--restart-recovery-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--verification-output", type=Path)
    args = parser.parse_args()

    _bind_source_sha_to_checkout(args.source_sha, repo_root=Path.cwd())

    output, base_digest = build_windows_package(
        args.exe,
        args.start_file,
        args.example_dir,
        args.diagnostic,
        args.accessibility_audit,
        args.keyboard_audit,
        args.restart_recovery_audit,
        args.output,
        args.source_sha,
    )
    binding = bind_portable_data_tool(
        output,
        args.data_exe,
        expected_base_package_sha256=base_digest,
    )
    verification = verify_portable_data_tool(output)
    package_sha = _require_verified_package_digest(binding, verification)
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
