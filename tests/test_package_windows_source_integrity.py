from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import package_windows


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Autosport Test")
    _git(repo, "config", "user.email", "autosport-test@example.invalid")
    (repo / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_package_boundary_accepts_exact_checkout(tmp_path: Path) -> None:
    repo, source_sha = _make_repo(tmp_path)

    package_windows._require_checkout_matches_exact_source(repo, source_sha)


def test_package_boundary_rejects_index_masked_raw_drift(tmp_path: Path) -> None:
    repo, source_sha = _make_repo(tmp_path)
    _git(repo, "update-index", "--assume-unchanged", "tracked.py")
    (repo / "tracked.py").write_text("VALUE = 9\n", encoding="utf-8", newline="\n")

    with pytest.raises(ValueError, match="assume-unchanged/skip-worktree masking"):
        package_windows._require_checkout_matches_exact_source(repo, source_sha)


def test_package_boundary_rejects_staged_index_drift(tmp_path: Path) -> None:
    repo, source_sha = _make_repo(tmp_path)
    (repo / "tracked.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "tracked.py")

    with pytest.raises(ValueError, match="index does not exactly match source_sha"):
        package_windows._require_checkout_matches_exact_source(repo, source_sha)


def test_package_boundary_ignores_mutable_clean_filter_for_raw_bytes(tmp_path: Path) -> None:
    repo, source_sha = _make_repo(tmp_path)
    _git(
        repo,
        "config",
        "filter.mask.clean",
        "python -c \"import sys;sys.stdout.write('VALUE = 1\\\\n')\"",
    )
    info_attributes = repo / ".git" / "info" / "attributes"
    info_attributes.write_text("tracked.py filter=mask\n", encoding="utf-8", newline="\n")
    (repo / "tracked.py").write_text("VALUE = 9\n", encoding="utf-8", newline="\n")

    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
    with pytest.raises(ValueError, match="raw tracked release source differs from source_sha"):
        package_windows._require_checkout_matches_exact_source(repo, source_sha)


def test_package_git_identity_ignores_inherited_git_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, source_sha = _make_repo(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))

    assert package_windows._git_output(repo, "rev-parse", "--verify", "HEAD") == source_sha
